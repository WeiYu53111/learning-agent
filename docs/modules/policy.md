# 教学策略详细设计

## 1. 输入输出与边界

策略为无副作用的确定性模块，接收版本化知识图、目标进度、学习者快照、会话检查点、用户意图及预算，输出一个 TeachingDecision。无需模型或数据库即可测试。

```text
build_plan(scope, graph, learner, limits) -> SessionPlan
choose_action(context) -> TeachingDecision
apply_outcome(plan, assessment_outcome) -> PlanUpdate
compute_blocked_targets(plan, skipped_targets) -> BlockedTargets
summarize_completion(plan, learner) -> CompletionResult
```

TeachingDecision 包含 action、objective_id、reason_code、简短理由、source_refs、assessment_ids、内容约束、是否需要新题、返回目标及 policy_version。LLM 可以提供错误解释和内容建议，不能直接覆盖动作枚举或完成条件。

## 2. 初始规划

从用户选择的章节/知识点取得 required 目标，展开 required 依赖，形成局部 DAG。目标身份必须来自发布版本。拓扑排序以教材章节顺序、目标顺序和 ID 稳定打破平局，不依赖模型返回顺序。

每个 SessionTarget 保存 origin（requested/prerequisite）、状态（pending/in_progress/satisfied/skipped/blocked/deferred）、进入次数和失败计数。计划保存 requested_scope 与 expanded_scope，报告不能把补基础的进度误算成用户目标全部完成。

未知必要前置进入诊断；未过期已达标目标标为 satisfied；过期前置安排 REVIEW。supporting 关系只进入例子和解释上下文。optional 目标只有用户主动选择后才成为本会话 required，不无限扩大学习范围。

规划最多展开 3 层；发现更深必要依赖时返回 scope_expansion_required，不静默忽略依赖。用户可明确扩大范围或缩小当前目标，不能在未验证基础上自动宣称达标。

## 3. 动作优先级

运行阶段合法性由应用服务先校验；策略不把 awaiting_answer 当成再次出题的机会。

| 优先级 | 条件 | 决策 |
|---|---|---|
| 1 | 暂停、退出或明确跳过 | 尊重用户意图，保存缺口 |
| 2 | 已达停止预算 | 标记 deferred，要求选择跳过/结束/仅换讲解 |
| 3 | 当前评估无效 | 换题 REASSESS，不重复原问题 |
| 4 | 确认必要前置缺口 | REMEDIATE，压入返回目标 |
| 5 | 关键前置未知或到期 | DIAGNOSE 或 REVIEW |
| 6 | 当前目标已达标且关键误解解除 | ADVANCE |
| 7 | 尚未诊断 | DIAGNOSE |
| 8 | 已知概念缺口 | EXPLAIN 或对比 EXAMPLE |
| 9 | 原理已懂、应用不足 | EXAMPLE 后 PRACTICE |
| 10 | 补救后需验证 | REASSESS |
| 11 | 计划处理完毕 | SUMMARIZE |

表中条件先归并成结构化教学状态，不仅按“总分低”选择动作。评估包含 error_kind：concept/reasoning/application/expression/question_issue/uncertain。表达差异但关键含义正确不能直接触发概念补救；uncertain 优先诊断。

## 4. 任务组合与问题选择

DIAGNOSE、PRACTICE、REASSESS、REVIEW 均生成一个主目标的一道新题；EXPLAIN、EXAMPLE、REMEDIATE 可生成讲解加一道检查题。第一版不一次生成任意数量题组，下一题取决于上一题结果。

诊断先检查最能区分当前状态的目标。未知概念先基础识别；概念有证据而原理未知则直接问为什么；已有原理证据而应用不足则给新情境。题目难度 1–5，初始按教材难度和显式水平建议，后续按当前目标证据调整，不继承无关上一题的难度。

一次出题只覆盖一个主目标，题目必须明确条件。新颖性组和最近已展示题传给生成器，应用层检查重复；疑似同题即使换字也不算第二次独立证据。

如果用户完全陌生且明确要求先讲，可以先 EXPLAIN，目标仍 unknown，直到取得有效证据。教材外知识不作为默认事实注入讲解；必要时明确说明教材缺口并缩小目标。

## 5. 补救、返回与预算

补救帧保存 parent_objective_id、prerequisite_id、trigger_assessment_id 和 resume_action。只补当前路径确有证据的缺口，多个后续目标失败只触发“诊断共同前置”，不能直接改其掌握分。

前置通过后弹出返回帧，回到原目标 REASSESS。前置被跳过时，将依赖目标 blocked，并继续其他独立目标；无可进行项时输出 ended_with_gaps。

每目标每会话累计最多 3 次有效失败，不因返回、刷新、换题而重置。invalid_question 不计失败，但最多替换 2 次；needs_review 连续两次无法解决时暂停目标，不能无上限复核换题。一次逻辑评分操作最多一次独立复核。

预算耗尽后“换讲法”只允许展示解释，不再自动发评分题；若希望继续评估，需显式开启新的学习会话，并展示已有失败历史。/skip 保留长期掌握状态，只结束本会话目标尝试。

## 6. 完成条件与决策记录

completed 要求本会话所有 requested required 目标及其必要依赖在决策时均满足当前证据标准。review_due、skipped、blocked、deferred 都不能充当 satisfied。

对满足标准的已有目标记录 ADVANCE 理由和证据，不强迫再教。全部目标已处理但有缺口时 ended_with_gaps；若仍有待答、待评估或待复核操作则不能结束。

每次决策保存输入摘要哈希、policy_version、目标状态、触发规则和证据 ID，既支持 `/why`，也用于离线回放。不要求保存模型隐式推理。

## 7. 测试

用同一知识 DAG 配置三类学习者，检查动作序列确实不同。重点验证规则优先级、共同前置不误降分、返回栈、跳过传播、独立分支继续、预算不重置、用户意图覆盖与完成条件。相同上下文应产生相同结构化决策。
