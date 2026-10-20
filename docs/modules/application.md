# 应用服务详细设计

## 当前实现：后端选择与恢复

实现位置：`application.py` 的 `Application.gateway(book, owner=None)`，依赖 `BackendRegistry` 和 `ModelBackend`。下文原有 Facade/Operation 签名保留为目标设计，当前没有完整 Operation 表和跨进程请求预算。

1. 新草稿以版本 ID、新会话以会话 ID 作为绑定所有者。新会话独立选择后端，不强制继承教材提取时的后端；克隆草稿同样按当时配置创建新绑定。
2. 所有者已有绑定且没有 `--backend` 时，直接读取保存的名称及 `BackendSpec` 快照，不重新解析默认后端。显式 `--backend` 则解析当前配置，写入新绑定，并记录 `backend_selected` 事件及前一个名称。
3. 绑定保存密钥变量名，不保存密钥值。HTTP 密钥在创建适配器时读取当前环境；Codex 身份由外部 CLI 当前登录决定。绑定不固定账户或代理，恢复时它们可能已经改变。
4. 同进程以 `(名称, 配置JSON)` 缓存适配器。应用通过统一的五类操作调用，不依赖 HTTP 返回体或 Codex 进程协议；测试仍支持注入假网关。
5. `resume` 对未结束会话先解析/保存后端选择，即使当前已有题目、不需要模型调用，也可明确切换；终态恢复只展示结果。`answer/next/reassess` 的模型操作使用会话绑定。
6. 评分前原始作答已提交。登录、网络、超时、契约失败都不写成功评分，不自动换后端；恢复沿用原 Attempt。重新评分失败保留旧评分；切换本身不重算旧成绩。

绑定及切换事件在独立短事务中提交，可能先于后续模型调用或业务对象的成功创建，因此 `backend_selected` 只表示选择已保存，不表示远端调用成功。新建对象之后的绑定失败/孤立绑定清理尚无专用工具。

提取批次记录 `batch_extracted` 的模型标签，审核增加 `audit_backend`，出题的 `question_presented` 带模型标签。每个评分修订保存 `名称/类型:模型`。未指定 Codex 模型时标签明确标记默认模型未知，不推测实际解析名称。完整逐调用用量及提示词版本追踪尚未实现。

当前恢复重新发起一次后端操作预算；原有下文“不因进程重启重置预算”的 Operation 方案属于后续目标。Codex 登录检查与执行时限独立，不能套用 HTTP 的四次请求预算，具体见[网关设计](llm.md)。

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
5. 进入内部规划循环，跳过有充分证据的目标；需要生成内容时取证据并校验，任务含题则保存 awaiting_answer，纯讲解则保存 feedback_ready，无剩余目标则保存总结和终态。

首题生成失败时保留 planning 会话及失败操作；resume 可重试，不创建第二个会话。所有目标已有有效证据时可直接总结，不能为满足流程强行出题。

## 3. 作答与评分

1. 读取会话，验证当前活动题、版本、阶段、未取消状态。
2. 短事务插入 Attempt 与待处理操作，按唯一作答递增目标总作答数，封存提示/答案暴露快照，将阶段设为 evaluating 并递增 revision。重试已有 Attempt 不再次计数。
3. 使用固定题目、rubric、原文及原回答调用网关。
4. 校验候选评估。输出不确定时在预算内独立复核一次；无效结果保持隔离。
5. 首次评分时 valid 证据进入掌握归约，其他状态只保存隔离证据、不新增掌握贡献。更新失败及待复核等结果计数，生成反馈；历史评分替换走第 7 节的完整重算流程。
6. 单写进程内短事务检查会话 revision、操作 execution_id 和取消标记，提交评估、投影、计数、事件及 feedback_ready。
7. 返回已落库反馈，等待确认继续。此用例不自动调用出题。

一道题最多一条原始 Attempt；显式评分复核创建 Assessment 修订，不重新插入 Attempt。若同题已有作答，返回已有反馈或未完成操作，不重新评分。

网关或契约失败时原始回答仍在，操作 failed_retryable，阶段保持 evaluating；恢复时从评估继续。失败不能被虚构成 valid 零分。invalid_question 可形成终结评估并进入 feedback_ready，再由策略换题。

## 4. 继续、提示与跳过

continue 只接受 feedback_ready 或恢复后的 planning。重新读取目标证据和依赖，执行内部规划循环。EXPLAIN/EXAMPLE/REMEDIATE 可附带一道新题；生成一个可展示任务后立即返回，不在用户尚未阅读反馈或作答时连续生成多个教学任务。

任务含题时提交为 awaiting_answer；纯讲解任务提交为 feedback_ready 并设置 pending_verification，等待确认后生成验证题。ADVANCE 更新游标并保持 planning，由同一次用例立即调用下一次策略决策，SUMMARIZE 写入终态；任何路径都不能在没有活动题时进入 awaiting_answer。

内部循环适用于 start、continue、skip 和 resume(planning)：每一步短事务保存决策及目标变化；遇到可交互任务、暂停或终态才返回稳定视图。循环上限为 `2 × 本会话目标数 + 8`，状态签名重复或没有进展时保存 paused 并返回 POLICY_STALLED。出题前检查总作答和分类预算，不能跳过最后已提交回答的评估。

hint 必须预先保存“该级提示已使用”的事件，提交成功才返回文字。若输出过程中崩溃，保守视为已展示；重试同一操作返回同级提示，不额外提升提示等级。

explain 与当前题冲突时，原子撤销题目、记录答案暴露和进入 planning，再生成讲解；生成失败不恢复该题的独立资格。

用户主动请求 explain 时生成纯讲解任务，禁止附带 question，返回 feedback_ready。有剩余评估预算才设置 pending_verification；预算耗尽保持 deferred，仅提供讲解/跳过/结束选项，不能借讲解绕过出题上限。

skip 将当前会话目标标为 skipped，撤销活动题并重新计算必要依赖阻塞；不改 ObjectiveMastery。pause 保存 resume_phase，不删题或回答，不解除持久化的补救计数。

## 5. 幂等与单写进程

Operation 状态为 `pending/running/succeeded/failed_retryable/failed_terminal/cancelled/superseded`。第一版对同一规范化数据目录取得操作系统级排他写锁，整个写命令或交互会话持锁，模型调用期间也不释放。第二写进程立即返回 OPERATION_BUSY；只读查询可独立运行。

不实现数据库租约、心跳或抢占。进程退出或崩溃由操作系统释放锁；新写进程取得锁后可将遗留 running 操作改为 failed_retryable(interrupted)，按检查点恢复。每次实际执行生成 execution_id，用于拒绝本进程取消之后返回的迟到模型结果，不用于分布式调度。

所有提交检查会话 revision、操作 execution_id 和状态仍为 running，发现过时快照即停止提交并重载。学习者 evidence_revision 用于投影可追溯和过时缓存检测；同一进程串行执行证据写入，无需跨进程投影冲突重试协议。

operation_id 相同且输入哈希相同：成功则返回保存的结果引用，运行中则展示状态，可重试则重新领取。相同 ID 但哈希不同一律 CONFLICT。

单次逻辑操作的请求次数与墙钟预算不会因进程重启重置；超过预算需用户明确创建新的重试操作，关联 retry_of，仍指向同一 Attempt，不能重复计分。

## 6. 恢复与取消

| 已持久化状态 | resume 行为 |
|---|---|
| planning，无活动题 | 恢复内部规划循环；需要生成时按操作预算执行 |
| awaiting_answer | 展示同一题，不调用模型 |
| evaluating，有 Attempt | 查评估操作；成功则展示反馈，否则按预算恢复 |
| feedback_ready | 展示已提交反馈，等待确认 |
| paused | 恢复 resume_phase，保留原始计数和题目 |
| 终态 | 只读展示总结 |

取消以数据库中的取消标记和会话 revision 为准，不依赖 HTTP 调用能否真正终止。先标记操作 cancelled、暂停会话，再取消本地请求；迟到结果不提交。恢复被取消的评分时创建新操作，使用已保存 Attempt。

已保存的决策、内容、反馈都不重生成。未提交模型候选不作为恢复依据。新知识版本发布不改变已有会话绑定；评分修订可更新长期投影，但不重写旧教学历史或已经结束的会话总结，展示时标明当前学习状态已更新。

## 7. 历史评分修订

reassess_attempt 是独立写用例，输入包括 attempt_id、reason、operation_id 和预期 current_assessment_id。已存在但尚未完成首次评分的作答走恢复，不伪装成历史修订。

1. 取得数据目录写锁，读取原回答、固定题目、rubric、知识版本和当前评估指针，保存修订操作；旧指针暂不变化。
2. 在事务外生成并校验新的评估候选；调用失败或契约失败时保留旧评估及所有旧投影，只记录操作失败。
3. 候选合法后，在内存证据流中替换旧修订。valid、needs_review 和 invalid_question 都执行重算；后两类撤销旧证据对分数、通过组和误解的贡献，不等于“保持掌握状态不变”。
4. 按原始作答时间重放受影响目标、误解关联目标和复习时间线。没有剩余有效证据时状态 unknown、估计 null，取消失去依据的复习项。
5. 一个事务内检查预期指针仍匹配，插入新修订、切换 current 指针、递增 evidence_revision、替换全部受影响投影、记录修订事件，并将未结束相关会话标记 needs_replan。任一步失败全部回滚，旧指针仍生效。
6. 返回改分前后差异及依据。已结束会话和旧决策保持历史原貌；未结束会话在下一次恢复/继续时重算 satisfied/blocked 和基于结果的失败计数。原始作答总数、已发生的题目替换和展示事件不减少。

若修订使当前活动题的必要前置失效，下一次恢复先撤销该活动题并重新规划；已提交的回答保留并完成评分，随后重规划，不删除原始证据。新修订为 needs_review 时保留待验证缺口，后续换新题；不自动无限重评同一作答。

## 8. 失败边界与测试

异常在 Facade 边界转换为公共 AppError，日志携带 operation_id。模型失败、磁盘不足和 CAS 冲突采用不同恢复提示。数据库提交结果不确定时先按操作 ID 查询，不能直接再次计分。

集成测试重点覆盖：每个提交前后的故障注入、第二写进程拒绝、持锁进程崩溃后恢复、取消后迟到响应、模型成功但提交失败、原始作答保存失败、输出前崩溃、重复 continue、valid→invalid_question/needs_review 原子撤销及修订失败回滚。
