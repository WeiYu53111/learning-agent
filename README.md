# PDF 教学 Agent

将文本 PDF 转成可追溯的知识清单和章节课程，支持提取审核、关联知识分析、练习反馈与暂停恢复。通过 CLI 准备和学习内容，通过 React Web 工作台检查原文、知识点及其关系。

目前为本地单用户原型。学习数据保存在 SQLite 中；模型生成的知识、题目和评分仍需结合原文判断。

## 安装

需要 Python 3.11+。构建 Web 前端还需要 Node.js 20.19+ 或 22.12+；构建完成后，运行 Web 无需 Node.js。

在项目根目录执行：

```bash
python -m venv .venv
```

激活虚拟环境：

```bash
# macOS / Linux
source .venv/bin/activate
```

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

安装应用及 Web 依赖：

```bash
python -m pip install -e ".[web]"
```

下文命令均在激活环境后执行。也可用 `python -m teaching_agent` 替代 `teach`。

## 快速开始：按章学习自己的 PDF

### 1. 配置模型后端

项目的 [teach.toml](teach.toml) 默认选择 `local_codex`，通过本机 Codex CLI 调用模型。请先确保 `codex` 已安装且已登录，并检查配置中的模型是否适用于你的账户。

也支持 HTTP API 后端。配置、认证与代理说明见 [模型后端文档](docs/backends.md)。

### 2. 导入教材，查看章节

```bash
teach import "教材.pdf"
teach books
teach outline BOOK
```

`BOOK` 是导入输出的教材 ID。下文的 `SECTION`、`VERSION`、`SESSION` 等也都是占位符，需替换为实际 ID。

导入只在本地解析 PDF、保存原文并建立检索索引，不调用模型。章节 ID 按解析顺序分配，前言和目录也可能占一个章节，因此 `s4` 不一定是书中第 4 章。以 `outline` 输出为准；页码均为 PDF 物理页码。

### 3. 索引到目标章

例如准备学习 `s4`：

```bash
teach index BOOK --section s4 --content-only
```

默认补齐 **s1～s4** 的内容提取、质量审核和章节摘要，无需先处理全书。已审核完成的章节会复用，中断任务中已保存的完整章节检查点也可复用。

`--content-only` 在内容就绪后结束，将关系分析留到学习时进行。省略该参数，会继续检查截至目标章的候选关系。

| 命令 | 行为 |
| --- | --- |
| `teach index BOOK --section s4 --content-only` | 补齐到 s4，跳过关系分析 |
| `teach index BOOK --section s5 --content-only` | 在已有内容上补齐到 s5 |
| `teach index BOOK --section s4 --new --content-only` | 重建 s4，补齐缺失前序章，保留其他已完成章 |
| `teach index BOOK --content-only` | 补齐全书内容 |
| `teach index BOOK --new --content-only` | 重新生成全书内容 |

提取会调用模型，包含原文分类、知识提取、审核，以及必要的修订、跨批处理和摘要；一个文本块不等于一次调用。长篇教材可能需要数百次请求，建议先索引到近期要学的章节。

### 4. 准备课程并开始学习

```bash
teach learn BOOK --section s4
```

目标章及前序章节内容就绪后，系统分析所需关系，再生成教学结构、题目与评分标准，执行审核和必要修订。交互终端会在准备完成后询问是否接受并开始学习。

学习时仅使用当前章及前序章节进行关系分析。章节关系分析默认最多调用 4 次、追溯 2 层前置知识；预算耗尽会提示继续或暂停。需要手动补充检查时：

```bash
teach relations BOOK --section s4 --max-calls 4 --max-depth 2
```

若决定在关系覆盖不完整时继续，可以使用 `learn ... --allow-partial-relations`。课程会保存当时的关系快照，后续补充知识不会改写已有课程。候选检查完成不代表关系完备，后续章节未参与分析也不表示没有关联。

### 5. 作答与恢复

回答支持多行输入，**单独输入 `/end` 提交当前小问**。多小问题目会逐问收集答案，再统一反馈。

| 学习中输入 | 用途 |
| --- | --- |
| `/hint` | 获取提示 |
| `/explain` | 请求讲解当前目标 |
| `/ask 问题` | 根据当前教材原文回答插问 |
| `/progress` | 查看学习进度 |
| `/cancel` | 清空当前未提交输入 |
| `/skip` | 跳过当前目标 |
| `/pause` | 保存并退出 |

下次继续：

```bash
teach sessions
teach resume SESSION
```

已生成课程可用 `teach start VERSION` 开始。掌握判定、评分复核和补练规则见 [评分与学习进度说明](docs/grading-issues.md)。

## Web：查看内容与关联知识

首次使用需构建前端：

```bash
npm --prefix web ci
npm --prefix web run build
teach web
```

打开 **http://127.0.0.1:8765**，Ctrl+C 停止服务。

工作台支持：

- 查看章节原文、摘要、内容清单和知识点，跳转并高亮引用。
- 查看前置知识、后续依赖、相关知识及一跳关系图。
- 切换索引版本，检查已审核章节、阶段结果和候选关系覆盖。

第一版仅提供查看，不触发模型调用或修改学习数据；发现、补充和管理功能留待后续版本。原文展示为解析后的文本，不是 PDF 阅读器。

导入或提取后点击“刷新数据”。页面状态反映已保存结果，不表示后台任务仍在运行。更新前端代码后需重新构建，更新后端代码后需重启服务。

```bash
# 指定数据目录与端口
teach --data-dir PATH web --port 8765
```

## 暂停、恢复与重建

模型等待期间，每 15 秒输出当前阶段及总耗时；这些提示不是新增调用，也不是完成百分比。配置中的 `timeout_seconds` 是单次后端执行时限，不是整本书的总时限。

Ctrl+C 可暂停任务。已保存结果保留，正在进行且尚未保存的请求可能需要重新执行。一个数据目录同一时间只运行一个写任务。

| 中断位置 | 恢复方式 |
| --- | --- |
| 知识提取 | 重复原 `index` 命令，不加 `--new` |
| 关系分析 | 重复相同范围的 `relations` 命令 |
| 课程准备或审核 | `teach prepare VERSION` |
| 学习或评分 | `teach resume SESSION` |

`--new` 表示创建新的内容或课程版本，不是普通重试。若已保存的语义审核明确要求重建，应按错误提示处理后再加 `--new`。评分失败时先恢复会话，无需重复作答。

## 常用命令

| 目的 | 命令 |
| --- | --- |
| 查询教材、章节与知识点 ID | `teach books` / `teach outline BOOK` |
| 查询课程版本、学习会话 | `teach drafts` / `teach sessions` |
| 查看课程和审核意见 | `teach inspect VERSION` |
| 从教材原文提问 | `teach ask BOOK --text "你的问题"` |
| 标记已掌握某章 | `teach mastery BOOK --section s4 --mark` |
| 撤销自报掌握 | `teach mastery BOOK --section s4 --clear` |
| 查看课程学习进度 | `teach progress VERSION` |
| 查询作答记录 | `teach attempts SESSION` |
| 复核一次评分 | `teach reassess ATTEMPT --reason "复核原因"` |

原文导入后即可 `ask`，无需先完成知识提取。问答依据当前书籍，可检索尚未学习的章节，不联网补充、不更新学习进度。自报掌握不产生分数，也不修改评分证据。

完整参数使用 `teach --help` 或 `teach <命令> --help` 查询。人工审核、修订、JSON 导入导出等高级操作见 [原型使用说明](docs/prototype.md)。

## 配置与数据

全局参数放在子命令之前：

```bash
teach --config teach.toml --backend local_codex index BOOK --section s4 --content-only
teach --data-dir PATH --env-file .env.cli --backend remote_api learn BOOK --section s4
```

- 默认数据目录为 `data-v2/`，可通过 `TEACH_DATA_DIR` 或 `--data-dir` 修改；后续操作应使用同一目录。
- `teach.toml` 保存可共享的后端配置；密钥放在环境变量或本地环境文件中。程序仅在指定 `--env-file` 时读取环境文件。
- 任务保存后端配置快照。修改模型、超时或切换后端后，恢复时显式传入 `--backend NAME` 才更新绑定；已完成结果仍会复用。
- 模型提取、审核、问答、出题和评分会把相应材料发送给所选服务；PDF 导入和 Web 查看在本地进行。
- 写入流程支持数据库增量升级至 schema 6；Web 为只读入口，不自动执行迁移。

旧提取索引不会作为新课程的内容入口，历史学习记录保留。需要清理无课程引用的旧索引时，先预览，再决定是否执行：

```bash
python -m teaching_agent.maintenance --data-dir data-v2 --book BOOK
# 备份数据库后执行清理；有课程引用时拒绝删除
python -m teaching_agent.maintenance --data-dir data-v2 --book BOOK --apply
```

普通删除操作：

```bash
teach delete VERSION         # 永久删除未发布草稿，保留教材
teach delete-book BOOK       # 有关联课程版本时拒绝删除
```

`delete-book BOOK --cascade` 会永久删除这本教材及其全部课程、会话、作答和评分，并删除数据目录内的 PDF 副本；原始导入文件和其他教材保留。

## 离线体验

无需模型配置或密钥：

```bash
teach demo
teach review VERSION
```

将 `VERSION` 替换为演示命令输出的 ID。在审核界面确认后依次输入 `approve`、`publish`，再运行 `teach start VERSION`。

演示使用固定教材、题目与关键词评分，只用于体验流程，不代表真实模型效果。

## 开发与验证

```bash
python -m pip install -e ".[dev,web]"
python -m pytest -q
ruff check src tests
npm --prefix web ci
npm --prefix web test
npm --prefix web run build
```

前端开发时先运行 `teach web`，再运行 `npm --prefix web run dev`；Vite 将 `/api` 代理到本地 8765 端口。

浏览器端到端测试：

```bash
cd web
npx playwright install chromium --no-shell
npm run test:e2e
```

端到端测试使用临时离线数据库及 8766 端口，不修改真实教材。默认使用项目 `.venv`；本地代理应放行 `127.0.0.1` 和 `localhost`。

## 文档与边界

| 文档 | 内容 |
| --- | --- |
| [计划与进展](docs/plans/README.md) | 当前任务、验收和后续改进 |
| [原型使用说明](docs/prototype.md) | 命令详解、审核流程和实现边界 |
| [模型后端](docs/backends.md) | 配置、认证、代理与后端切换 |
| [知识提取方案](docs/plans/knowledge-extraction.md) | 语义提取、审核及索引策略 |
| [关系分析方案](docs/relation-on-demand-plan.md) | 候选检查、预算和课程快照 |
| [Web 方案](docs/plans/web-app.md) | 只读工作台范围与验收 |
| [评分规则](docs/grading-issues.md) | 掌握判定、复核与已知问题 |
| [总体设计](docs/design.md) | 架构与模块职责 |

目前支持文本 PDF，不支持 OCR、Web 写操作、向量数据库或多用户并发写入。知识关系由候选筛选与模型检查得到，不保证穷举；离线测试通过也不等于真实教材的语义质量已经验收。
