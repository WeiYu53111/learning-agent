# 学习者模型详细设计

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

计算函数是纯函数，时间 as_of 显式传入，便于回放。应用服务负责读历史、提交结果和并发检查。投影保留 consumed_assessment_ids、rule_version 与 evidence_revision，能够说明依据。

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

状态计算顺序：无可计分证据为 unknown；补救标记或确认误解为 needs_remediation；达到初步掌握门槛后根据到期时间区分 provisionally_mastered/review_due；其他为 learning。未知估计为 null，不保存伪造的零分。

例如独立 1.0、独立 1.0 后初步掌握；随后独立 0.0，估计降为 0.6 并进入补救。后续独立 1.0、1.0 得到 0.76、0.856，第二次新场景通过后重新达到数值与证据门槛。

## 4. 误解生命周期

误解码属于知识版本中的受控目录，模型不能将临时自由文本直接存为永久标签。新候选误解附描述和回答摘录，先保存候选，不自动影响长期状态。

一个场景的有效支持证据产生 suspected；两个不同场景确认后为 confirmed。解除必须由明确考查该误解的题目标记 refutes_code，且两道新场景独立通过。普通题答对不能默认解除所有误解。

同一证据若支持和反驳冲突进入复核。已解决误解再次出现时重新打开，重新累计本轮支持证据，保留过去的确认与解除时间。

误解涉及多个目标时，只有被确认且标记 critical 的关联目标打开补救。一个应用题错误不能自动降低所有前置目标掌握；策略可以要求诊断前置，等待新证据。

## 5. 复习调度

初次初步掌握创建 interval_stage=0，due_at 为达标时间加 1 天。通过复习依次进入 3、7、14 天阶段；达到 14 天后保持 14 天，不无限翻倍。基准时间为实际复测提交时间。

到期是查询时根据 as_of 派生的状态，无需后台定时任务。`teach review` 默认按必要依赖拓扑顺序、due_at、objective_id 排序。同一个目标只保留一个当前待复习项。

有效独立复习通过可沿用已有掌握证据，若数值仍 ≥ 0.8 且无补救/误解则推进间隔；失败立即退出复习达标，打开补救。辅助通过不推进间隔，仍等待独立复测。

失败后重新达到初步掌握条件时将间隔重置为 1 天。歧义题、无效题和失败模型请求不修改到期日。历史评分修订后依据有效时间线重建 due_at，不使用“重算发生时间”重置复习。

## 6. 报告与偏好

Progress 按版本分别报告 required_total、verified、review_due、learning、unknown，以及会话级 skipped/blocked。review_due 不计为当前充分达标；已覆盖包含看过讲解或作过题，不代表通过。

解释包含最近可计分证据、独立/辅助条件、关键误解与达标缺口，不向用户输出虚假的概率置信度。维度汇总展示已验证目标数及缺口，不用平均分替代目标证据。

第一版仅支持会话级显式偏好，例如“先原理后例子”“简短讲解”。偏好只进入内容生成，不降低评分阈值，不建立自动推断的长期人格标签。

## 7. 修订与测试

修订评分时重建该用户目标时间线，以及受影响误解关联的目标投影。提交需检查统一 learner evidence_revision，避免并发新证据丢失。已完成会话历史不改写，当前报告标明状态由新评估更新。

测试使用可手算证据序列，验证乱序修订、辅助不解除补救、同场景不重复计数、无效证据隔离、误解解除、复习时钟、跨目标隔离与完整重放一致性。
