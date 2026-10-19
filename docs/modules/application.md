# 应用服务详细设计

## 1. 用例边界

应用服务连接 CLI、纯领域规则和外部适配器。负责身份和版本解析、读取快照、调用策略、执行模型任务、校验输出、组织事务及恢复。领域规则不访问 I/O；网关不拥有数据写入权。

核心接口以类型化命令接收 operation_id、session_id（如适用）、expected_revision（如适用）及业务输入：

```text
import_document(ImportCommand) -> ImportView
publish_knowledge(PublishCommand) -> KnowledgeView
start_session(StartCommand) -> SessionView
submit_answer(SubmitAnswerCommand) -> FeedbackView
continue_session(ContinueCommand) -> SessionView
request_hint(HintCommand) -> SessionView
request_explanation(ExplainCommand) -> SessionView
skip_objective(SkipCommand) -> SessionView
pause_session(PauseCommand) -> SessionView
resume_session(ResumeCommand) -> SessionView
reassess_attempt(ReassessCommand) -> FeedbackView
start_review(ReviewCommand) -> SessionView
get_progress(ProgressQuery) -> ProgressView
```

写接口均幂等，读接口不生成模型请求。返回对象不暴露未提交题目的答案和 rubric。

## 2. 会话开始

1. 解析教材和发布版本，校验章节或知识点属于版本。
2. 读取必要目标、依赖及学习者投影；在同一读取快照记录投影修订号。
3. 调用策略生成目标计划；存在知识质量或依赖问题时在创建会话前拒绝。
4. 创建 active/planning 会话、目标列表和启动操作；唯一操作 ID 防止重复开课。
5. 选择首个动作，取证据并生成必要内容；校验后一次保存决策、任务、题目和 awaiting_answer 检查点。

首题生成失败时保留 planning 会话及失败操作；resume 可重试，不创建第二个会话。所有目标已有有效证据时可直接总结，不能为满足流程强行出题。

## 3. 作答与评分

1. 读取会话，验证当前活动题、版本、阶段、未取消状态。
2. 短事务插入 Attempt 与待处理操作，封存提示/答案暴露快照，将阶段设为 evaluating 并递增 revision。
3. 使用固定题目、rubric、原文及原回答调用网关。
4. 校验候选评估。输出不确定时在预算内独立复核一次；无效结果保持隔离。
5. 对 valid 证据调用学习者归约器，得出目标、误解、复习变化；其他状态不更新掌握。
6. 短事务检查会话 revision、操作持有者、取消标记和学习者投影 revision，提交评估、投影、事件及 feedback_ready。
7. 返回已落库反馈，等待确认继续。此用例不自动调用出题。

一道题最多一条原始 Attempt；显式评分复核创建 Assessment 修订，不重新插入 Attempt。若同题已有作答，返回已有反馈或未完成操作，不重新评分。

网关或契约失败时原始回答仍在，操作 failed_retryable，阶段保持 evaluating；恢复时从评估继续。失败不能被虚构成 valid 零分。invalid_question 可形成终结评估并进入 feedback_ready，再由策略换题。

## 4. 继续、提示与跳过

continue 只接受 feedback_ready 或无活动题的 planning。重新读取目标证据和依赖，执行一次策略决策。EXPLAIN/EXAMPLE/REMEDIATE 后可同时准备一份新题，但只能激活这一道；展示反馈后不能同时连续推进多个目标。

任务含题时提交为 awaiting_answer；纯讲解任务提交为 feedback_ready，等待确认后再决定练习。ADVANCE 只更新目标游标并回到 planning，SUMMARIZE 写入终态；任何路径都不能在没有活动题时进入 awaiting_answer。

hint 必须预先保存“该级提示已使用”的事件，提交成功才返回文字。若输出过程中崩溃，保守视为已展示；重试同一操作返回同级提示，不额外提升提示等级。

explain 与当前题冲突时，原子撤销题目、记录答案暴露和进入 planning，再生成讲解；生成失败不恢复该题的独立资格。

skip 将当前会话目标标为 skipped，撤销活动题并重新计算必要依赖阻塞；不改 ObjectiveMastery。pause 保存 resume_phase，不删题或回答，不解除持久化的补救计数。

## 5. 幂等、并发和租约

Operation 状态为 `pending/running/succeeded/failed_retryable/failed_terminal/cancelled/superseded`。持有者标识为随机 worker_token，租约 120 秒，运行中每 15 秒独立续租；模型调用总预算不得阻止续租线程运行。

领取采用短事务比较 revision，写入 token 与 lease_until。租约内第二执行者返回 OPERATION_BUSY。过期后可用新 token 重新领取；迟到旧执行者提交时因 token 不匹配被拒绝。

所有提交同时检查会话 revision 和操作 token。多个会话影响同一学习目标时，还检查 learner_projection_revisions 的 revision；冲突后重新读取证据，在新事务内重算投影，最多重试 3 次，不重复调用模型。

operation_id 相同且输入哈希相同：成功则返回保存的结果引用，运行中则展示状态，可重试则重新领取。相同 ID 但哈希不同一律 CONFLICT。

单次逻辑操作的请求次数与墙钟预算不会因进程重启重置；超过预算需用户明确创建新的重试操作，关联 retry_of，仍指向同一 Attempt，不能重复计分。

## 6. 恢复与取消

| 已持久化状态 | resume 行为 |
|---|---|
| planning，无活动题 | 恢复或重新领取生成操作 |
| awaiting_answer | 展示同一题，不调用模型 |
| evaluating，有 Attempt | 查评估操作；成功则展示反馈，否则按预算恢复 |
| feedback_ready | 展示已提交反馈，等待确认 |
| paused | 恢复 resume_phase，保留原始计数和题目 |
| 终态 | 只读展示总结 |

取消以数据库中的取消标记和会话 revision 为准，不依赖 HTTP 调用能否真正终止。先标记操作 cancelled、暂停会话，再取消本地请求；迟到结果不提交。恢复被取消的评分时创建新操作，使用已保存 Attempt。

已保存的决策、内容、反馈都不重生成。未提交模型候选不作为恢复依据。新知识版本发布不改变已有会话绑定；评分修订可更新长期投影，但不重写旧教学历史或已经结束的会话总结，展示时标明当前学习状态已更新。

## 7. 失败边界与测试

异常在 Facade 边界转换为公共 AppError，日志携带 operation_id。模型失败、磁盘不足和 CAS 冲突采用不同恢复提示。数据库提交结果不确定时先按操作 ID 查询，不能直接再次计分。

集成测试重点覆盖：每个提交前后的故障注入、并发会话投影冲突、租约抢占、取消后迟到响应、模型成功但提交失败、原始作答保存失败、输出前崩溃及重复 continue。
