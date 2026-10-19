# LLM 网关详细设计

## 1. 责任与接口

封装不同模型服务的调用差异，提供提取知识、生成教学任务、评估回答和独立复核能力。网关不修改业务状态，不执行模型生成代码，不把模型建议当作事实直接写入学习者模型。

```text
extract_knowledge(ExtractionInput, CallBudget) -> ExtractionCandidate
generate_task(TeachingInput, CallBudget) -> TaskCandidate
grade_answer(AssessmentInput, CallBudget) -> AssessmentCandidate
review_assessment(ReviewInput, CallBudget) -> AssessmentCandidate
```

适配器协议：`complete(messages, output_contract, generation_profile, deadline) -> ModelResult`。ModelResult 含结构化候选、finish_reason、供应商请求 ID、模型实际标识、用量、耗时；缺少的元数据标为 unknown，不凭空估计精确值。

## 2. 输入和上下文

| 能力 | 必需输入 | 禁止或避免输入 |
|---|---|---|
| 知识提取 | 原文片段、章节、候选临时 key、提取 schema | 用户学习水平、完整学习历史 |
| 教学生成 | 当前动作、主目标、rubric、证据、薄弱处、已见题和显式偏好 | 无关历史、其他版本教材 |
| 回答评估 | 固定题目、原 rubric、原文、原始回答 | 用户历史总分、模型预期用户能力 |
| 独立复核 | 同样的题目、rubric、原文和回答、冲突字段名 | 第一轮结论和分数，避免锚定 |

评估不输入用户画像，避免“过去答得好所以这次也判对”。提示与答案暴露由程序用于证据分类，不能让模型自行推断是否独立。

提示词由版本化系统规则、任务契约、明确分隔的数据组成。教材及回答中的指令只作为数据。先发送完成任务所需证据，再发送最小用户上下文；不把整本教材或完整聊天历史塞入每次调用。

## 3. 输出契约

### 知识提取

```text
points[]:
  temp_key, title, description
  objectives[]:
    temp_key, description, dimension, required
    rubric_items[]: key, description, critical, weight, source_refs[]
    suggested_question_types[], source_refs[]
edges[]: prerequisite_temp_key, dependent_temp_key, kind, source_refs[]
coverage: processed_chunk_ids[], unresolved_items[]
```

权重为正，题目 rubric 权重合计必须为 1，允许浮点误差 1e-6。知识目标 rubric 表达达标要点；具体题目可以细化，但不能改变该目标的关键能力要求。模型缺少依据时输出 unresolved，而不是补造引用。

### 教学任务

```text
objective_id, action, explanation?, example?, source_refs[]
question?:
  stem, question_type, difficulty
  reference_answer, rubric_items[], hints[]
  scenario_summary, suggested_novelty_group, source_refs[]
```

问题型动作必须有 question，解释型动作允许附一道检查题；ADVANCE/SUMMARIZE 不走教学内容生成接口。objective_id 和 action 必须与输入相等。question 不包含模型生成的数据库 ID。

rubric_items 包含 key、description、critical、weight、reference_points 和来源。hints 最多 3 级，由轻提示到具体引导，不能直接输出标准答案；若提示实际暴露答案，需在审核中标记相应暴露级别。

模型建议的新颖性分组须与历史场景比较后由程序确认。题目不因分组字符串不同就被视为新题。

### 回答评估

```text
status: valid | needs_review | invalid_question
item_results[]:
  rubric_key, credit: 0 | 0.5 | 1
  answer_spans[]: start, end, quote
  reason, issue?
error_kind: concept | reasoning | application | expression | question_issue | uncertain
misconception_candidates[]: code?, description, supporting_spans[]
feedback, correction?, question_issue?
```

最终 score 由程序按固定 rubric 权重加权计算，passed 由 score ≥ 0.8 且全部 critical 项 credit=1 推导，不要求模型额外决定 is_correct。存在关键项缺失却达到高分等不一致时进入 needs_review。

回答区间引用必须匹配原始回答；未出现的要点可以标 missing，无须伪造摘录。合理同义表达允许 credit=1。valid 必须给出每个 rubric 项恰好一条结果；重复、漏项或未知 key 拒绝。credit=0 的缺失项允许空 spans，已覆盖项应有回答证据。

invalid_question 必须解释题目问题，score 为 null；needs_review 不写入数值掌握。语义判断无法完全由格式验证保证，固定人工评估集用于检测系统性误判。

## 4. 校验流程

1. 检查 finish_reason；截断响应不能作为完整结果接受。
2. 优先使用服务支持的结构化输出；否则要求纯 JSON，最多接受一个完整 JSON 对象。
3. 执行严格类型、长度、枚举、字段完整性检查；拒绝重复 JSON 键、NaN 和 Infinity，不从多份冲突 JSON 中随意取第一份。
4. 校验目标、动作和版本与输入一致；候选引用必须来自输入允许的片段集。
5. 校验引用区间、rubric 权重、回答摘录和关键要点一致性。
6. 返回候选，由应用层完成领域检查和状态提交。

格式修复使用同一任务、原证据和安全错误字段列表，不拼接服务端堆栈或密钥。禁止静默截断、夹紧分数、删除不合法目标后假装完整成功。原始响应不写常规日志；只保存安全诊断、长度、哈希及 request_id。

## 5. 预算与重试

每个网关逻辑调用共用 CallBudget：max_requests=4、deadline=180 秒；每次 HTTP 请求上限 min(90 秒, 剩余时间)。格式修复最多 1 次，网络/限流额外重试最多 2 次，所有分支都消耗同一计数，不能嵌套乘出更多请求。

用户作答的独立复核属于同一评分操作，共享剩余 CallBudget；主评分已用尽预算时不得额外复核，应暂停并明确需要用户发起后续重试。教材长文按批执行，每批独立操作并记录，不能把全书处理混同一个 180 秒请求任务。

网络瞬断、429、服务端暂时错误可重试；凭证错误、不支持参数、无效模型和无效地址不原样重试。遵守合理 Retry-After，在剩余预算内用 1/2/4 秒退避和抖动；等待时间超预算则暂停。SDK 自动重试关闭，避免隐藏次数。

上下文预算按模型能力配置，而不是按字符数假定。预留输出和安全余量；精确 tokenizer 不可用时用保守上界预检，服务端仍报超限则拆分输入或拒绝，不能静默删关键证据。输出截断在剩余预算内用更小任务重试，不无限提高输出上限。

## 6. 供应商与测试适配

GenerationProfile 声明模型、结构化输出能力、上下文上限、输出上限、支持的温度或推理参数。未验证参数不发送；供应商特有字段只在适配器出现。endpoint 及超时经过配置校验，日志中的 URL 去除凭证和敏感查询参数。

假适配器按 fixture 返回合法、非法、截断和超时结果，可注入耗时与取消。契约测试覆盖所有公开能力。真实模型评估单独运行，记录模型、提示词和用量，不把一次真实模型输出固定成普遍正确答案。
