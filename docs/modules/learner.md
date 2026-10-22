# 学习者模型详细设计

> 2026-10-22 版本说明：本文下方的数值评分、失败清零与原作答状态机描述保留为 v1 设计记录。新作答已使用 v2 核心要求判断、失败减一次、定向缺口解除和最多两轮追问；旧作答及其复核沿用 v1。当前实现与边界以[评分进度记录](../grading-issues.md)为准。

后端同步（当前实现）：候选评分来自 `ModelBackend.grade`；评分修订保存 `名称/类型:模型标签`，未解析的 Codex 默认模型明确标为 unknown。后端切换本身不更新掌握、误解或复习；显式重新评分才新增修订并按有效证据重放。评分失败保留旧结果，提示及独立性规则不随后端改变。完整规则/提示词版本元数据仍是后续目标。

## 1. 职责与接口

以不可变原始作答、当前评估修订和展示事件为依据，生成目标级掌握、误解和复习投影。模型不出题，不决定下一教学动作。

```text
get_snapshot(learner_id, objective_ids, as_of) -> LearnerSnapshot
classify_evidence(attempt, assessment, exposures) -> EvidenceClassification
rebuild_objective(evidence_stream, rule_version, as_of) -> ObjectiveProjection
rebuild_misconception(evidence_stream, code) -> MisconceptionProjection
plan_review(projection, review_history, as_of) -> ReviewProjection
explain_mastery(objective_id) -> EvidenceSummary
```

计算函数是纯函数，时间 as_of 显式传入，便于回放。应用服务在单写进程内负责读取历史和原子提交。投影保留 consumed_assessment_ids、rule_version 与 evidence_revision，能够说明依据并发现使用了过时快照。

## 2. 证据分类

| 分类 | 条件 | 数值更新 | 独立通过 |
|---|---|---|---|
| independent | valid、新题、无提示、无答案暴露 | 使用原分数 | 满足通过规则才计入 |
| assisted | valid、新题、已用提示、无答案暴露 | 使用 0.7 × 得分 | 不计入 |
| practice_only | 同题重复、同已见场景、已展示答案 | 不更新 | 不计入 |
| quarantined | needs_review/invalid_question | 不更新 | 不计入 |

独立是证据条件，不是能力维度。Question 的 novelty_group 表示问题的核心解法与场景结构；同一题换措辞不能得到新分组。程序比较已见分组，模型提供相似性建议，疑似重复保守视为 practice_only。

取证时点为 Attempt 提交时，之后展示参考解释不追溯改变之前的独立性。取消尚未展示的题也保存 seen/exposure 事件，后续复用保守处理，避免刷新获得“未见题”。

证据按 Attempt.submitted_at、Attempt.id 稳定排序，不按改分时间排序。每作答只选择 current_assessment_revision。人工修订为无效时，该证据从数值序列移除，但保留审计历史。

## 3. 掌握归约

依照[领域规则](../domain-and-policy.md)计算。归约状态除展示字段外还保留：last_independent_result、remediation_open、distinct_pass_groups、last_valid_evidence_at、last_independent_at。

独立失败打开 remediation_open，清空自最近失败以来的 distinct_pass_groups。新的独立通过关闭“最近失败”标记，但已确认关键误解仍保持补救状态；初步掌握仍要求失败后累计两个不同分组独立通过。历史通过不抵消新失败。

辅助回答只影响数值，不能清除补救标记。无效题不打开补救、不清空通过计数。practice_only 不影响任何通过/失败计数。

状态计算顺序：无可计分证据为 unknown；补救标记或确认误解为 needs_remediation；最近独立失败之后已有两个不同分组独立通过，则根据到期时间区分 provisionally_mastered/review_due；其他为 learning。趋势估计不参与状态判断。未知估计为 null，不保存伪造的零分。

例如独立 1.0、独立 1.0 后初步掌握；随后独立 0.0，估计降为 0.6 并进入补救。后续独立 1.0、1.0 得到 0.76、0.856，第二次新场景通过后重新满足证据门槛。另一序列 0、0.8、0.8（后两题全部关键项通过）也在第三题初步掌握，即使趋势估计只有 0.512。

## 4. 误解生命周期

误解码属于知识版本中的受控目录，模型不能将临时自由文本直接存为永久标签。新候选误解附描述和回答摘录，先保存候选，不自动影响长期状态。

题目保存 `targeted_misconception_codes`，评估保存 `misconception_results[] = {code, relation, rubric_keys, answer_spans, reason}`，relation 为 supports/refutes/inconclusive。code 必须属于本知识版本目录，rubric_keys 必须属于当前题；未知码作为候选保存，不能进入受控状态归约。

一个场景的有效 supports 证据产生 suspected；两个不同场景确认后为 confirmed。refutes 只在 code 属于题目 targeted_misconception_codes、评估 valid、整题独立通过且有对应 rubric 和回答证据时有效；同一轮误解至少两道不同新场景满足该条件后转 resolved。普通题通过、assisted、practice_only 和 quarantined 均不增加解除计数。

同一评估每个 code 恰好一个结果；同时支持和反驳或证据不一致进入复核。每条新的有效 supports 重置该码的当前解除组计数，inconclusive 不增减计数。已解决误解再次出现时重新打开，重新累计本轮支持证据，保留过去的确认与解除时间。

误解涉及多个目标时，只有被确认且标记 critical 的关联目标打开补救。一个应用题错误不能自动降低所有前置目标掌握；策略可以要求诊断前置，等待新证据。

## 5. 复习调度

初次初步掌握创建 interval_stage=0，due_at 为达标时间加 1 天。通过复习依次进入 3、7、14 天阶段；达到 14 天后保持 14 天，不无限翻倍。基准时间为实际复测提交时间。

到期是查询时根据 as_of 派生的状态，无需后台定时任务。`teach review` 默认按必要依赖拓扑顺序、due_at、objective_id 排序。同一个目标只保留一个当前待复习项。

有效独立复习通过可沿用已有掌握证据，无新的补救或关键误解时推进间隔，不检查平滑趋势分数；失败立即退出复习达标，打开补救。辅助通过不推进间隔，仍等待独立复测。

失败后重新达到初步掌握条件时将间隔重置为 1 天。歧义题、无效题和失败模型请求不修改到期日。历史评分修订后依据有效时间线重建 due_at，不使用“重算发生时间”重置复习。

## 6. 报告与偏好

Progress 按版本分别报告 required_total、verified、review_due、learning、unknown，以及会话级 skipped/blocked。review_due 不计为当前充分达标；已覆盖包含看过讲解或作过题，不代表通过。

解释包含最近可计分证据、独立/辅助条件、关键误解与达标缺口，不向用户输出虚假的概率置信度。维度汇总展示已验证目标数及缺口，不用平均分替代目标证据。

第一版仅支持会话级显式偏好，例如“先原理后例子”“简短讲解”。偏好只进入内容生成，不降低评分阈值，不建立自动推断的长期人格标签。

## 7. 修订与测试

修订评分时重建该用户目标时间线，以及受影响误解关联的目标投影。无论新修订是 valid、needs_review 还是 invalid_question，都先替换归约输入中的旧修订再重放；后两类移除旧分数、通过和误解影响，不是保持旧投影不变。若已无有效证据，回到 unknown、估计 null，并取消失去依据的当前复习安排。

提交在单写进程的一个事务内切换 current 指针并递增 learner evidence_revision，所有受影响投影记录新版本。已完成会话历史不改写，当前报告标明状态由新评估更新。未结束会话的目标缓存标记需重新计算，下一次继续时依据新投影更新 satisfied/blocked 等派生状态。

测试使用可手算证据序列，验证 0→0.8→0.8 达标、乱序修订、valid→invalid_question/needs_review 撤销影响、辅助不解除补救、同场景不重复计数、普通通过不解除误解、两次有针对性反驳解除误解、复习时钟、跨目标隔离与完整重放一致性。
