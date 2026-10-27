# 可替换模型后端

教学模块通过 `ModelBackend` 的 `book_operation` 执行内容分类、知识提取、语义审核、跨批审查以及教学结构/题目生成；旧 `extract` 操作已删除。自动准备与独立修订使用 `revise_patch` 返回局部修改包；新版知识提取纠错使用同一 `book_index` 操作并携带候选和错误。HTTP API 和 Codex CLI 共用契约及提示词，离线演示不用于语义质量验收。教材审核、掌握判定、数据库和恢复仍由 teach 管理。

## 1. 选择配置

根目录的 `teach.toml` 已纳入版本管理，提供项目默认值：local_codex、gpt-6-astra、600 秒执行时限，不包含凭据。`teach.toml.example` 保留通用模板，仅在配置文件缺失时复制或用于参考，避免覆盖已有设置。程序默认从当前工作目录读取，也可以显式传入路径：

```powershell
.venv\Scripts\teach.exe --config teach.toml --backend local_codex start VERSION
```

`--backend` 优先于 `active_backend`。新会话会保存选中后端的名称和不含密钥的配置快照；后续 `resume/answer/next/reassess` 使用保存的配置，不随默认后端改变。显式传入 `--backend` 会更新该会话的绑定，并记录切换事件。更新同名后端配置后，也需要显式选择才能让已有会话采用新配置。

草稿同样保存绑定，提取恢复、审核和校正沿用它；新建教学会话按当时的后端选择建立自己的绑定。克隆新草稿按当时配置选择后端。后端失败不会自动回退到另一个服务。

没有配置文件、没有显式选择时，原有 `TEACH_*` 环境变量继续提供 HTTP 后端。内置演示教材默认使用离线演示；需要验证真实模型时可对演示教材显式指定 Codex 或 HTTP 后端。离线后端不能用于普通 PDF。

## 2. Codex 使用已有登录

```toml
active_backend = "local_codex"

[backends.local_codex]
type = "codex_cli"
executable = "codex"
auth = "existing_login"
timeout_seconds = 180
# model = "本机账户可用的模型名称"
```

先在终端执行 `codex login status`；尚未登录时自行运行 `codex login`。teach 不读取、复制或修改 Codex 凭据，不提供登录、注销或账户切换功能。ChatGPT 登录和 API Key 登录均由 Codex 管理，teach 使用当前可用身份及其额度。

每次操作先检查登录状态，再通过 `codex exec` 启动独立调用。任务经标准输入传入，使用 `--output-schema` 约束最终 JSON，并从 `--output-last-message` 文件读取结果，再做领域校验。日志输出不能当作评分结果；退出码非零、结果缺失或契约错误均视为失败。

调用在临时目录运行，使用 `--ephemeral`、只读沙箱、关闭 shell 工具，并用 `--ignore-user-config` 避免个人插件、MCP 和项目配置干扰教学任务。凭据仍由 Codex 使用。**因此未指定 `model` 时使用 Codex 内置默认模型，不继承个人 `config.toml` 的模型设置；需要固定模型时请在 teach.toml 中指定。** 需要支持这些参数的 Codex CLI；本机已核对版本为 0.163.0。

teach 不从最终文本猜测真实模型名称。指定模型时记录所选模型；未指定时记录 `codex-default (resolved model unknown)`。这表示默认模型未解析，不代表一个固定模型版本。

登录检查最长 15 秒；推理子进程使用 `timeout_seconds`。超时后终止直接子进程并清理临时文件，不自动重试付费调用。评分失败时原始作答仍保留，可通过 `resume` 重试；提取失败用 `draft --resume`。审核失败可重新执行 `audit`。

## 3. 代理

Codex 登录检查和推理子进程都继承 teach 的环境变量。需要本地代理时，在启动 teach 的同一个 PowerShell 会话设置：

```powershell
$env:HTTP_PROXY = "http://127.0.0.1:7897"
$env:HTTPS_PROXY = "http://127.0.0.1:7897"
.venv\Scripts\teach.exe --config teach.toml --backend local_codex start VERSION
```

也可在本地环境文件写入这两个变量，并通过 `--env-file .env.cli` 显式读取。已存在的进程环境变量优先于环境文件。代理地址不写死在应用中，也不保存到会话的后端配置快照；恢复时使用当时进程的网络环境。HTTP API 后端同样使用 HTTPX 的环境代理支持。

## 4. HTTP API

```toml
[backends.remote_api]
type = "openai_compatible"
base_url = "https://api.openai.com/v1"
model = "your-model"
api_key_env = "TEACH_API_KEY"
timeout_seconds = 180
request_timeout_seconds = 90
input_chars = 48000
output_tokens = 8000
```

密钥从 `api_key_env` 指定的环境变量读取，TOML 和数据库不保存密钥。切换示例：

```powershell
.venv\Scripts\teach.exe --env-file .env.cli --backend remote_api resume SESSION
```

`timeout_seconds` 是 HTTP 操作预算，`request_timeout_seconds` 是单次请求超时；两者取较小值限制每次请求。原有格式修复和网络重试规则保留。`input_chars` 对 HTTP/Codex 都有效；`output_tokens` 与 `request_timeout_seconds` 仅用于 HTTP。

项目 `teach.toml` 中 `local_codex` 的 `input_chars` 设置为 96000，以容纳合并后的草稿修订输入。未配置时仍默认 48000。Codex 按请求数据和输出 JSON Schema 的字符数检查；局部修订仍输入完整草稿，DraftPatch 的 Schema 也比审核报告大，因此仍可能出现审核成功而修订超限。超限提示给出实际字符数和上限；调整配置后使用 `--backend local_codex` 显式覆盖旧绑定。这个值是应用侧限制，不是模型上下文容量承诺。

`revise_patch` 成功后提供 `last_revision_metrics`：输入数据字符数、紧凑 JSON 修改包与完整草稿字符数、调用耗时。应用保存这些计数，不额外打印原始请求。该指标不含提示词及 Schema，不等同于输入上限检查值或实际 token 用量。网络或契约失败不产生成功指标，不回退到全量修订。

## 5. 数据和验收

首次写操作将数据库从版本 1 升级到版本 2，新增 `backend_bindings` 表，保留教材、版本、作答和评分。旧版本或会话此前没有后端快照，会在首次需要绑定时使用当时的后端选择；建议为旧会话显式指定后端。只读命令不会触发迁移。

绑定保存非敏感配置，后端切换记录事件；提取批次、审核和题目展示事件记录后端/模型标签。每次评分修订的 `model` 字段记录 `名称/类型:模型`，旧评分保持原样。`teach attempts SESSION` 可以查看评分来源。改变后端不自动重算旧成绩。

自动测试覆盖命名配置、环境变量兼容、密钥不进入快照、代理继承、Codex 失败与超时、JSON 结果解析、临时目录清理、数据库迁移及跨进程恢复时的后端选择。在线调用验证只说明认证和传输链路可用，实际教学质量仍需人工审核教材和评分。

本次完整回归：41 项测试通过，Ruff 检查通过。

2026-10-20：使用本机 ChatGPT 已有登录及 `http://127.0.0.1:7897` 代理，完成一次真实 Codex 空教材审核调用，返回 `{"issues":[]}` 并通过契约校验。未设置代理的首次调用达到 90 秒超时；后续独立诊断显示 WebSocket 重连后回退 HTTPS。此结果不等同于真实教材的提取与评分质量已验收。

## 6. 提示词维护

模型提示文本位于 `src/teaching_agent/prompts/`，随安装包发布：

- `common.txt`：所有操作共用的系统约束。
- `source_analysis.txt`、`book_index.txt`、`extraction_review.txt`、`boundary_review.txt`：唯一的新知识提取流程；`audit.txt` 用于教学草稿审核。旧 `extract.txt` 已删除。
- `revise.txt`、`revise_patch.txt`：全量与局部修订。
- `task.txt`、`grade.txt`：旧版出题与评分，保留用于兼容。
- `task_evidence.txt`、`grade_evidence.txt`：新版核心要求出题与能力评价，支持缺口覆盖和局部追问。
- `codex_data.txt`：Codex 后端的任务数据隔离提示。

`prompt_loader.py` 通过 `importlib.resources` 按 UTF-8 加载固定名称的资源，不依赖启动目录。文件末尾的换行会被移除，内部空格和换行保留；代码在原有位置添加消息分隔和动态 JSON Schema。资源缺失、无法读取或为空会明确报错。

2026-10-22 的独立化仅迁移文本，发送内容保持不变。`tests/fixtures/prompt_baseline.json` 保存迁移前六类操作的完整消息和 Codex 包装文本；`tests/test_prompts.py` 验证逐字符一致、工作目录独立性及加载失败处理。修改提示规则后应审核基线差异并同步更新测试预期，不能仅为通过测试覆盖基线；语义效果仍需另行建设真实模型评估集验证。本次未增加提示版本持久化或自定义提示目录。

新作答保存规则版本和学习目标快照。真实后端使用 `task_evidence` / `grade_evidence`；旧作答复核继续调用 `grade`，保留旧版分数规则。离线演示支持新版流程，但仅进行关键词匹配，不能用于评价真实语义质量。
