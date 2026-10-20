# PDF 教学 Agent

可以运行的 CLI 教学原型（0.2.0）。导入文本 PDF，生成章节知识清单，人工审核校正后发布，再进行诊断、练习、补救和复习。SQLite 保存教材、审核记录、作答和评分修订，支持暂停恢复。

完整的[已实现功能清单、命令和实现边界](docs/prototype.md)已按当前代码整理。当前从指定的已发布教材版本开始教学，尚不支持 `teach ask` 或 `/ask` 自由提问；学习中的普通输入会作为当前题目的答案处理。

## 立即体验

当前工作区的 `.venv` 已安装应用。在 PowerShell 中运行：

```powershell
.venv\Scripts\teach.exe demo
```

命令生成三页 Python 教材及待审核草稿，输出版本 ID。将下列 `VERSION` 替换为实际 ID：

```powershell
.venv\Scripts\teach.exe review VERSION
```

检查知识点、教学顺序、原文引用和代表题；输入 `approve`，再输入 `publish`。然后开始对话：

```powershell
.venv\Scripts\teach.exe start VERSION
```

直接输入答案，反馈后回车继续。`/hint` 获取提示，`/explain` 请求讲解，`/skip` 跳过，`/progress` 查看进度，`/pause` 保存退出。`teach sessions` 查询会话，`teach resume SESSION` 恢复。未激活虚拟环境时，所有 `teach` 命令均使用上述完整路径。

`.venv\Scripts\teach.exe` 是安装工具根据 `pyproject.toml` 的命令入口生成的启动程序，业务代码位于 `src/teaching_agent/`。也可先激活虚拟环境，再使用短命令：

```powershell
.\.venv\Scripts\Activate.ps1
teach demo
```

激活后也可用 `python -m teaching_agent demo`；无需激活的等价写法为 `.venv\Scripts\python.exe -m teaching_agent demo`。

**离线演示无需密钥，但使用固定场景和关键词评分，仅用于体验流程，不代表真实模型的教学效果。** 演示每个目标只有三种题目，反复练习可能耗尽题目。

## 使用自己的 PDF

模型后端现在可选 **Codex CLI（复用已有登录）** 或 **HTTP API**，配置和切换方式见[模型后端说明](docs/backends.md)。选择 Codex 时无需填写 HTTP API 密钥：

```powershell
Copy-Item teach.toml.example teach.toml
codex login status
$env:HTTP_PROXY = "http://127.0.0.1:7897"   # 本机需要代理时设置
$env:HTTPS_PROXY = "http://127.0.0.1:7897"
.venv\Scripts\teach.exe --backend local_codex draft BOOK --section s1 --pages 1:5
```

导入 PDF 仍使用下方的 `import` 命令。生成草稿后执行 `audit`、`review`，确认发布后用 `--backend local_codex start VERSION` 开始学习。下方 `.env` 示例用于 HTTP API 后端；未创建 `teach.toml` 时继续兼容原有环境变量配置。

参考 `.env.example` 配置 `TEACH_API_BASE`、`TEACH_API_KEY`、`TEACH_MODEL`。已有 `.env` 不自动读取或覆盖，需要显式指定 `--env-file`。模型服务需要兼容 Chat Completions HTTP 接口。

```powershell
.venv\Scripts\teach.exe import "教材.pdf"
.venv\Scripts\teach.exe outline BOOK
.venv\Scripts\teach.exe --env-file .env draft BOOK --section s1 --pages 1:5
.venv\Scripts\teach.exe --env-file .env audit VERSION
.venv\Scripts\teach.exe --env-file .env review VERSION
.venv\Scripts\teach.exe --env-file .env start VERSION
```

`BOOK`、`VERSION` 替换为输出的 ID；`--pages` 是可选的 PDF 物理页范围，必须位于所选章节内。建议先用一小节检验模型效果。导入仅在本地解析；生成草稿、审核、自然语言校正、出题和评分会将相应材料发送给配置的模型服务。

默认数据目录 `data-v2/`，首次写入自动建新库，不迁移旧数据库。全局参数 `--data-dir PATH` 可隔离不同实验。

## 安装与验证

新环境需要 Python 3.11 或更新版本：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check src tests
```

测试覆盖审核门禁、原文引用、先修关系、掌握判定、提示降权、评分修订、会话恢复、单写进程锁，以及真实 CLI 子进程的完整离线教学流程。真实模型的知识划分、题目质量和语义评分仍需实际教材验收。

## 文档

- [原型使用说明与实现边界](docs/prototype.md)：当前可执行命令、审核流程和设计取舍。
- [模型后端与认证](docs/backends.md)：Codex、HTTP API、已有登录、代理、后端切换与数据库迁移。
- [总体设计](docs/design.md)：目标架构和模块职责。
- [领域模型与教学状态机](docs/domain-and-policy.md)：证据、评分和教学策略。
- [实施与验收](docs/implementation-plan.md)：完整版本的验收目标。
- [各模块详细设计](docs/modules/README.md)：完整设计契约。

当前接口以原型使用说明和 `teach --help` 为准。暂不支持 Web、OCR、向量数据库或多用户并发写入。
