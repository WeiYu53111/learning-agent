# PDF 教学 Agent（第一版）

这是一个基于 smolagents 的文本型 PDF 教学 MVP。用户上传教材并选择系统识别出的章节后，系统会像教师一样逐个讲解知识点：先讲当前知识点并提问，评估和纠正回答，再根据本次学习表现调整下一个知识点的教学内容。每个知识点的掌握情况会持久化到 SQLite。

## 已实现

- 文本型 PDF 上传、校验和持久化
- 拒绝扫描件、加密或损坏的 PDF
- 优先读取 PDF 书签，缺少书签时根据标题编号和字体识别章节
- 章节原文预览及页码映射
- 逐知识点生成教学内容，由模型根据知识点复杂度自主决定检查题数量
- 回答评估、错误纠正、误区记录和掌握度更新
- 根据上一步评估结果自适应生成下一个知识点的讲解
- SQLite 跨会话学习记忆
- 可直接使用的 Web 界面和 OpenAPI 接口

第一版不支持 OCR、图片文字、复杂图表理解、多用户登录和生产级权限系统。

## 快速启动

建议使用 Python 3.11 或更高版本：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
uvicorn app.main:app --reload
```

项目已包含模型客户端所需的 SOCKS 代理支持；即使终端设置了 `ALL_PROXY`、`HTTPS_PROXY`
等 SOCKS 地址，也无需额外安装 `socksio`。

打开 <http://127.0.0.1:8000>。

程序启动时会自动读取项目目录中的 `.env`。已经导出的进程环境变量优先于 `.env` 中的同名配置。
也可以直接在终端设置变量：

```bash
export OPENAI_API_KEY='你的密钥'
export LLM_MODEL_ID='gpt-4o-mini'
uvicorn app.main:app --reload
```

`OPENAI_API_KEY` 是必填配置。未配置时应用会拒绝启动，不会回退到本地规则生成。

后端默认输出 `INFO` 级别的请求、备课阶段和模型调用日志。需要更详细或更精简的日志时，
可在 `.env` 中设置 `LOG_LEVEL=DEBUG|INFO|WARNING|ERROR|CRITICAL`。模型结构化输出解析失败时，
日志会包含请求阶段、响应长度、内容哈希、截断预览和异常堆栈，但不会记录 API Key 或完整教材。

## 使用兼容 OpenAI 的模型服务

```bash
export OPENAI_API_KEY='服务密钥'
export LLM_MODEL_ID='模型名称'
export LLM_API_BASE='https://服务地址/v1'
uvicorn app.main:app --reload
```

例如使用 DeepSeek 的 OpenAI 兼容接口：

```bash
export OPENAI_API_KEY='你的 DeepSeek API 密钥'
export LLM_MODEL_ID='deepseek-v4-pro'
export LLM_API_BASE='https://api.deepseek.com'
```

当前代码使用 OpenAI 兼容协议，因此不能把 `LLM_API_BASE` 配置为
`https://api.deepseek.com/anthropic`。

使用 DeepSeek 时，程序会为知识点提取、教学文案、练习题和答案评分开启思考模式。
推理强度可通过 `LLM_REASONING_EFFORT=low|high|max` 配置，默认为 `high`。
模型输出上限可通过 `LLM_MAX_TOKENS` 配置，默认为 `16000`，用于降低长 JSON 被截断的概率。

模型调用使用 smolagents 的 `OpenAIModel`。后端直接读取用户指定章节，并将其作为不可执行的
教材证据交给模型，不执行模型生成的代码。

## API 主流程

```text
POST /api/documents
GET  /api/documents/{document_id}/sections
GET  /api/sections/{section_id}
GET  /api/sections/{section_id}/download
POST /api/sections/{section_id}/prepare
GET  /api/sessions/{session_id}?user_id={user_id}
GET  /api/users/{user_id}/sessions/latest
POST /api/questions/{question_id}/attempts
POST /api/sessions/{session_id}/next
GET  /api/users/{user_id}/progress
```

`prepare` 只生成第一个知识点的讲解和模型认为必要的检查题，不接受人工设置题目数量。当前知识点的题目全部作答后，用户才能确认继续并由客户端调用 `next`；后端会综合这些题目的评估与学习记录生成下一个知识点。重复这一过程，直到本章完成。

浏览器会保存最近一次学习会话的标识。刷新页面后，客户端通过会话查询接口从 SQLite
恢复已备课程、当前步骤、已提交答案、评估反馈和掌握度；教材与答案本身不会写入浏览器存储。
修复前创建、尚无浏览器会话标识的课程，可以在页面输入学习者 ID 后点击“恢复最近课程”。

交互式接口文档位于 <http://127.0.0.1:8000/docs>。

## 运行测试

```bash
pytest
ruff check .
```

## 数据目录

默认运行数据位于：

```text
data/
├── learning_agent.db
└── uploads/
```

可以通过 `DATABASE_PATH`、`UPLOAD_DIR` 和 `MAX_UPLOAD_SIZE_MB` 修改。

## 当前掌握度规则

- 使用提示会降低本次成绩对掌握度的贡献。
- 答错或掌握度低于 45% 时标记为 `needs_remediation`。
- 至少两次连续独立答对且掌握度达到 75% 后，标记为 `provisionally_mastered`。
- 初步掌握后创建一天后的复习时间。

这套规则便于解释和测试，后续可以替换成 Bayesian Knowledge Tracing 等正式模型。

