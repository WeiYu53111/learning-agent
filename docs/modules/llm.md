# LLM 网关详细设计

## 1. 责任与接口

当前接口定义于 `backends.py` 的 `ModelBackend(Protocol)`。网关提供提取、审核、校正、出题和评分，不修改业务状态，不把模型建议直接写入学习者模型。独立双模型复核尚未实现；`reassess` 仍调用 `grade`，由应用层新增评分修订。

```text
model: str
extract(chunks) -> Draft
audit(draft, chunks) -> Audit
revise(draft, chunks, instruction) -> Draft
task(objective, chunks, action, history, misconceptions) -> Task
grade(question, answer, chunks, misconceptions) -> Assessment
```

`BackendRegistry.build(BackendSpec)` 创建 `Gateway`（HTTP）、`CodexGateway` 或 `DemoGateway`。HTTP/Codex 继承 `ModelOperations`，共用任务提示与输入封装，分别实现 `call(operation,payload,contract)`。离线演示按固定教材返回候选，应用禁止它处理普通 PDF。选择规则与绑定属于应用层，网关不负责回退或更换服务。

当前直接返回 Pydantic 候选，不提供通用 `ModelResult` 或完整用量遥测。`model` 是来源标签；应用加上后端名称与类型。配置的模型名不保证是服务解析后的精确版本；Codex 未指定模型时明确记录 `codex-default (resolved model unknown)`。统一请求 ID、实际模型、耗时与用量封装属于后续扩展。

## 2. 输入和上下文

| 能力 | 必需输入 | 禁止或避免输入 |
|---|---|---|
| 知识提取 | 原文片段、章节、候选临时 key、提取 schema | 用户学习水平、完整学习历史 |
| 草稿审核 | 当前草稿及范围内完整原文 | 学习者掌握状态 |
| 草稿校正 | 当前草稿、范围内原文、人工修改要求 | 无关教材或学习历史 |
| 教学生成 | 当前动作、主目标、rubric、证据、待验证误解码、已见题和显式偏好 | 无关历史、其他版本教材 |
| 回答评估 | 固定题目、原 rubric、原文、原始回答、题目针对的误解定义 | 用户历史总分、模型预期用户能力 |
| 显式重新评分 | 原题、rubric、原文和原回答，经 `grade` 重新调用 | 不因旧结论而默认认可；reason 当前只作修订记录 |

评估不输入用户画像，避免“过去答得好所以这次也判对”。提示与答案暴露由程序用于证据分类，不能让模型自行推断是否独立。

提示词由版本化系统规则、任务契约、明确分隔的数据组成。教材及回答中的指令只作为数据。先发送完成任务所需证据，再发送最小用户上下文；不把整本教材或完整聊天历史塞入每次调用。

## 3. 输出契约

当前字段以 `models.py` 为准：

| 返回类型 | 当前字段 |
| --- | --- |
| Draft | `points/objectives`、`content_items`、`misconceptions`、`notes`；目标含 `prerequisites/source_refs/sample_question/excluded_reason` |
| Audit | `issues[]`，每项含 `description/severity/source_refs` |
| Task | `objective_key/action/explanation/question` |
| Question | `stem/reference_answer/rubric/hints/novelty_group/source_refs/targeted_misconception_codes` |
| Assessment | `status/items/feedback/correction/misconception_results`；items 使用 `answer_quotes` |

当前 SourceRef 为 `chunk_id/quote`，按原文逐字且唯一匹配；尚未存 start/end。`EXPLAIN` 必须无题；其余已实现问题动作必须有题。语义新颖性尚无独立证明器，程序拒绝已见题面或题组，不能仅据此宣称场景在语义上独立。

以下三个子节保留更丰富的**目标输出契约**，其中独立引用偏移、题型/难度、误解候选等字段不能直接传给当前接口（当前模型禁止额外字段）。

### 知识提取

```text
points[]:
  temp_key, title, description
  objectives[]:
    temp_key, description, dimension, required
    rubric_items[]: key, description, critical, weight, source_refs[]
    suggested_question_types[], source_refs[]
edges[]: prerequisite_temp_key, dependent_temp_key, kind, source_refs[]
misconceptions[]:
  temp_key, description, related_objective_keys[], critical_objective_keys[], source_refs[]
coverage: processed_chunk_ids[], unresolved_items[]
```

权重为正，题目 rubric 权重合计必须为 1，允许浮点误差 1e-6。知识目标 rubric 表达达标要点；具体题目可以细化，但不能改变该目标的关键能力要求。模型缺少依据时输出 unresolved，而不是补造引用。

误解定义与目标一并经过知识质量校验和发布，由程序分配版本内受控 code。提取出的候选不能直接标记某个用户存在误解；用户状态只能来自有效作答证据。

### 教学任务

```text
objective_id, action, explanation?, example?, source_refs[]
question?:
  stem, question_type, difficulty
  reference_answer, rubric_items[], hints[]
  scenario_summary, suggested_novelty_group, source_refs[]
  targeted_misconception_codes[]
```

问题型动作必须有 question，解释型动作允许附一道检查题；应用输入 question_mode=forbidden 时必须无 question（用于用户主动讲解或预算耗尽），required 时必须有一道题，optional 时允许零或一道。ADVANCE/SUMMARIZE 不走教学内容生成接口。objective_id 和 action 必须与输入相等。question 不包含模型生成的数据库 ID。

rubric_items 包含 key、description、critical、weight、reference_points 和来源。hints 最多 3 级，由轻提示到具体引导，不能直接输出标准答案；若提示实际暴露答案，需在审核中标记相应暴露级别。

模型建议的新颖性分组须与历史场景比较后由程序确认。题目不因分组字符串不同就被视为新题。

targeted_misconception_codes 必须是输入允许的同版本误解码，正常题可为空；策略要求验证的码必须包含，且 rubric 中必须有区分该误解的关键要点。无法设计可靠题目时报告生成失败，不用空数组绕过解除验证。

### 回答评估

```text
status: valid | needs_review | invalid_question
item_results[]:
  rubric_key, credit: 0 | 0.5 | 1
  answer_spans[]: start, end, quote
  reason, issue?
error_kind: concept | reasoning | application | expression | question_issue | uncertain
misconception_results[]:
  code, relation: supports | refutes | inconclusive
  rubric_keys[], answer_spans[], reason
misconception_candidates[]: code?, description, supporting_spans[]
feedback, correction?, question_issue?
```

最终 score 由程序按固定 rubric 权重加权计算，passed 由 score ≥ 0.8 且全部 critical 项 credit=1 推导，不要求模型额外决定 is_correct。存在关键项缺失却达到高分等不一致时进入 needs_review。

回答区间引用必须匹配原始回答；未出现的要点可以标 missing，无须伪造摘录。合理同义表达允许 credit=1。valid 必须给出每个 rubric 项恰好一条结果；重复、漏项或未知 key 拒绝。credit=0 的缺失项允许空 spans，已覆盖项应有回答证据。

invalid_question 必须解释题目问题，score 为 null；needs_review 不写入数值掌握。语义判断无法完全由格式验证保证，固定人工评估集用于检测系统性误判。

每个 targeted_misconception_code 必须恰有一个 misconception_results 项；未涉及的受控码可以额外返回 supports，但不可返回 refutes。rubric_keys 和回答区间必须有效，supports/refutes 要有支持结论的证据；无法区分时必须 inconclusive，不根据“答案正确”补造反驳。

refutes 只有在评估 valid、题目针对该码且整题独立通过时才由学习者模块计数；网关不知道提示展示历史，不能自己宣布误解解除。未知误解继续放入 candidates，未经目录确认不得冒充受控码。

## 4. 校验流程

1. 检查 finish_reason；截断响应不能作为完整结果接受。
2. 优先使用服务支持的结构化输出；否则要求纯 JSON，最多接受一个完整 JSON 对象。
3. 执行严格类型、长度、枚举、字段完整性检查；拒绝重复 JSON 键、NaN 和 Infinity，不从多份冲突 JSON 中随意取第一份。
4. 校验目标、动作和版本与输入一致；候选引用必须来自输入允许的片段集。
5. 校验引用区间、rubric 权重、回答摘录和关键要点一致性。
6. 返回候选，由应用层完成领域检查和状态提交。

格式修复使用同一任务、原证据和安全错误字段列表，不拼接服务端堆栈或密钥。禁止静默截断、夹紧分数、删除不合法目标后假装完整成功。原始响应不写常规日志；只保存安全诊断、长度、哈希及 request_id。

## 5. 预算与重试

当前预算按传输区分：

| 后端 | 当前预算与失败处理 |
| --- | --- |
| HTTP | 操作默认 180 秒，单请求默认 90 秒且不超过剩余预算；最多 4 次请求，最多 1 次格式修复、2 次网络额外重试 |
| Codex | 登录检查单独最多 15 秒，执行子进程默认最多 180 秒；teach 不自动重试，CLI 自身网络重连受进程时限约束 |
| 演示 | 无远端请求与认证 |

HTTP 网络异常、429、5xx 可重试，其他 HTTP 错误直接失败；当前退避为 2、4 秒并受剩余预算限制，尚未实现 Retry-After 和抖动。Codex 非零退出不暴露原始 stderr，仅返回安全错误及退出码。两者均不自动切换后端。

当前 `input_chars` 默认 48000，检查序列化任务数据与 schema 的字符总量，并非 token 精确预算，也不含固定指令全部开销；超限拒绝，不截断证据。HTTP `output_tokens` 默认 8000；该字段不传入 Codex。显式恢复会开启新调用预算，尚无跨重启持久预算或自动独立复核。精确 tokenizer、按模型容量规划及完整调用遥测属于后续目标。

## 6. 供应商与测试适配

HTTP 使用 HTTPX POST `/chat/completions`，通过系统提示提供 JSON Schema，检查响应完整性、严格 JSON 与 Pydantic 契约，再由应用执行领域校验。`api_key_env` 指定密钥变量，不存密钥值；HTTPX 使用进程环境代理。

Codex 调用流程：

1. 通过 `shutil.which(executable)` 查找 CLI，预检输入大小，创建操作专属临时目录。
2. 运行 `codex login status`；失败提示用户自行登录，不读取凭据文件，不实现账户管理。
3. 将 Pydantic schema 转为严格 schema：递归移除 default、要求对象全部属性显式返回、禁止额外属性；不修改领域原始 schema。
4. 使用参数列表启动 `codex exec --ephemeral --sandbox read-only --skip-git-repo-check --ignore-user-config -c features.shell_tool=false --output-schema FILE --output-last-message FILE --color never -`。提示和任务通过 stdin 传入，显式模型通过 `--model` 传入。
5. 子进程复制进程环境，继承 `HTTP_PROXY/HTTPS_PROXY`；Windows 隐藏启动窗口。临时工作目录隔离项目上下文，忽略个人配置，因此默认模型为 CLI 内置默认而非个人配置的模型。
6. 只接受成功退出且存在、不超过 4 MiB 的结果文件；严格解析 JSON 和 Pydantic 候选。退出码成功本身不足以判定成功，stdout/stderr 不作为业务结果。
7. 结束后清理临时目录；超时由 subprocess 终止直接子进程。尚不提供通用进程树清理或完整逐调用审计。

自动测试用假进程验证认证失败、非零退出、结果缺失、非法 JSON/契约、超时、参数及代理继承和临时目录清理，回归 HTTP 与离线流程。真实 Codex 最小调用已用已有登录及本地代理返回合法 Audit；这不代表教材提取、出题和评分质量已验收。详见[后端验证记录](../backends.md)。
