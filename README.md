# PDF 教学 Agent

可以运行的 CLI 教学原型（0.2.0）。导入文本 PDF，生成章节知识清单，自动审核并最多修订两轮，确认学习范围后发布，再进行诊断、练习、补救和复习。SQLite 保存教材、审核记录、作答和评分修订，支持暂停恢复。

完整的[已实现功能清单、命令和实现边界](docs/prototype.md)已按当前代码整理。支持全书知识梳理、按章两阶段教学准备、用户自报掌握以及教材内 `teach ask` / `/ask` 自由提问。普通输入仍作为当前题目的答案处理；插问请使用 `/ask 问题`。


## Web 内容与关联知识检查

第一版 Web 为本地只读工作台，可查看已保存的原文、章节摘要、内容清单、知识点，以及前置知识、后续依赖和相关知识。支持一跳关系图、跨章跳转、引用高亮、索引切换和阶段结果查看。不会触发模型调用或修改学习数据；关联发现、补充与管理留待后续版本。

在项目根目录安装 Python Web 依赖，再构建 React 前端（构建环境需要 Node.js 20.19+ 或 22.12+，运行时无需 Node.js）：

```bash
python -m pip install -e ".[web]"
npm --prefix web ci
npm --prefix web run build
teach web
```

浏览器打开 **http://127.0.0.1:8765**。也可运行 `.venv/bin/python -m teaching_agent web`（macOS/Linux），或 `.venv\Scripts\python.exe -m teaching_agent web`（Windows）。Ctrl+C 停止服务。

```bash
teach --data-dir PATH web --port 8765
```

数据目录沿用 CLI 默认值 `data-v2/` 或 `TEACH_DATA_DIR`。没有教材时页面显示导入说明；导入或提取后点击“刷新数据”。Web 不自动迁移数据库。页面状态表示已保存数据，不表示后台任务仍在运行；候选检查完成也不代表知识关系完备。首版原文查看使用已解析文本，不包含 PDF 阅读器或 OCR。

前端开发：保持 `teach web` 在 8765 端口运行，再运行 `npm --prefix web run dev`；Vite 代理 `/api`。验证命令：

```bash
python -m pip install -e ".[dev,web]"
python -m pytest -q
npm --prefix web test
npm --prefix web run build
cd web
npx playwright install chromium --no-shell
npm run test:e2e
```

浏览器验收使用临时离线数据库及 8766 端口，不修改真实教材。默认使用项目 `.venv`；测试环境需要绕过代理访问 localhost。方案与验证记录见 [Web 专项计划](docs/plans/web-app.md)。

## 全书梳理、按章学习与自由提问

知识提取已统一到新版语义流程：先识别目录/出版信息与正文，再按主题组织批次、提取和审核知识点，最后处理跨批边界。旧提取入口和旧检查点不再执行，历史数据保留。只有旧索引时，运行 `teach index BOOK` 从原文重新构建；新掌握状态不继承旧版。

`teach index BOOK --new` 强制全量重建；已有新版全书索引后，可用 `teach index BOOK --section SECTION --new` 仅重建一章，失败后去掉 `--new` 恢复。其他章节复用，涉及目标章的知识关系重新分析。真实教材的 `draft` 要求先完成新版索引，避免隐式全书处理。详见[提取优化与验收计划](docs/plans/knowledge-extraction.md)。

直接运行 `teach` 或 `teach --help` 可查看中文命令导航、分组说明和快速开始步骤；运行 `teach learn --help` 等可查看具体参数及示例。

```bash
teach import "教材.pdf"
teach books
teach index BOOK
teach outline BOOK
teach learn BOOK --section s5
```

`import` 只解析文本并建立本地原文检索索引，不调用模型。`index` 分批梳理内容清单、知识点、章节摘要及全书关系，不生成题目；再次执行可恢复或复用结果，`index BOOK --new` 创建新版本。无文本页面会明确列出，目前不支持 OCR。

关系分析先用本地词项检索筛选候选：每个知识点最多选择 3 个同章和 3 个跨章匹配，并补充同章相邻知识点；模型仅验证指定候选对，不再遍历所有摘要组组合。每批最多 32 对，按约 12,000 字符的请求预算分批（单对过长时单独发送）。日志显示候选数和验证批次进度，候选计划及每批结果均保存检查点。旧任务恢复时保留已完成提取、摘要和关系结果，已分析过的分组组合覆盖的候选无需重复请求。词项候选可能漏掉没有共同词语的隐含关系，并非全书关系穷举或完整性保证。已经运行的进程不会自动切换算法；按 Ctrl+C 后重新执行同一 `index BOOK` 命令生效，无需 `--new`。

`learn` 先生成并校验整章教学结构，再按目标组生成代表题及评分标准，最后进入已有审核、确认和发布流程。中断后重复相同命令，或使用显示的 `prepare VERSION` 恢复；`learn BOOK --section s5 --new` 显式重新生成。已有已发布版本优先恢复未结束会话。旧 `draft` 流程仍可使用。

进入章节时，未掌握的跨章前置知识可选择补学、我已掌握或本次跳过。自报掌握不产生分数、不修改评分证据，可以撤销：

```bash
teach mastery BOOK --section s4 --mark
teach mastery BOOK --point POINT --mark
teach mastery BOOK --section s4 --clear
```

`POINT` 使用 `outline` 显示的知识点 ID。自报标记按知识内容指纹复用，知识发生变化后需要重新确认；本次跳过只记录在当前会话。

```bash
teach ask BOOK --text "为什么工具定义变化会影响缓存？"
```

学习中使用 `/ask 问题`。问答仅依据当前书籍的原文，支持跨章检索和未学习章节，附物理页码及引用；不联网补充，不更新学习进度。原文导入后即可提问，无需等待 `index`；提问不会自动启动全书或整章梳理、出题。证据不足会明确说明。

相关检查点、模型调用耗时和失败原因保存在本地 SQLite 中。数据库自动增量升级至 schema 5，旧教材、会话和评分保留。首次全书梳理仍可能耗时较长；本地词项检索可能遗漏同义表达，真实教材的质量和耗时仍需对照验证。完整流程见[全书学习方案与实现状态](docs/plans/book-learning.md)。


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

下面按 **Codex CLI 后端** 给出完整流程。命令在项目根目录执行，先激活虚拟环境。`BOOK`、`VERSION`、`SESSION`、`SECTION` 都是占位符，需要替换成实际 ID；不需要凭记忆输入，查询方式见下方。

### 1. 准备配置、登录和代理

```powershell
.\.venv\Scripts\Activate.ps1

# 仓库已提供 teach.toml；仅文件缺失时从模板恢复
if (-not (Test-Path teach.toml)) {
    Copy-Item teach.toml.example teach.toml
}

codex login status
# 如果未登录，在终端运行 codex login

# 本机需要代理时设置；新开的 PowerShell 需重新设置
$env:HTTP_PROXY = "http://127.0.0.1:7897"
$env:HTTPS_PROXY = "http://127.0.0.1:7897"
```

代理地址是本机示例，其他电脑应按实际代理设置。也可以写入本地 `.env.cli`，启动时显式传入 `--env-file .env.cli`；程序不会自动读取 `.env`。Codex 继承 teach 的进程环境，teach 不管理账户或复制凭据。

仓库中的 `teach.toml` 纳入版本管理，默认使用 Codex、`gpt-6-astra` 和 600 秒执行时限。模板仍提供未指定模型、180 秒的起始配置。下面是项目默认配置；模型可用性取决于当前账户：

```toml
active_backend = "local_codex"

[backends.local_codex]
type = "codex_cli"
executable = "codex"
auth = "existing_login"
timeout_seconds = 600
model = "gpt-6-astra"
```

配置文件缺失或从其他目录运行、且没有命名后端选择时，程序会兼容旧环境变量并选择 HTTP。建议从项目根目录运行，或用 `--config PATH` 指定文件。未指定 model 时使用 Codex 内置默认模型，不继承个人 Codex 配置中的模型。`teach.toml` 只保存可共享设置；API 密钥、代理及认证信息仍通过本机环境或被忽略的 `.env` 文件提供。

### 2. 导入后先查章节

```powershell
teach import "D:\教材\你的教材.pdf"
teach books
teach outline BOOK
```

`s1/s2/s3` 是解析生成的章节 ID，不等于书中的第 1/2/3 章。例如本次导入的《AI Agents in Depth》：

| 章节 ID | 标题 | PDF 物理页码 |
| --- | --- | --- |
| s1 | 前言与目录 | 1–8 |
| s2 | 引言 | 9–14 |
| s3 | 第 1 章 AI Agent 入门 | 15–37 |

这只是本次教材的例子，其他文件以 `outline` 输出为准。`--pages` 使用 PDF 物理页码，不是正文印刷页码，且范围必须在所选章节内。

### 3. 自动准备内容与失败恢复

```powershell
teach --backend local_codex draft BOOK --section SECTION
```

第一次建议处理较小范围，例如上述教材第一章的前五页：

```powershell
teach --backend local_codex draft BOOK --section s3 --pages 15:19
```

出现“草稿：ver_...；失败后可用 draft --resume 继续”表示草稿 ID 已保存，**不代表提取已经完成，也不说明实际后端**。随后会显示批次数、PDF 页范围、实际后端/模型和提取、校验、自动校正、保存阶段；长时间等待时每 15 秒输出当前阶段等待时间及总耗时。恢复时会标明复用已保存批次。进度以普通文本实时写入 stderr，不代表模型生成百分比，也不缩短实际调用时间；不要在另一个终端同时发起写命令。

失败后恢复同一个草稿：

```powershell
teach --backend local_codex draft --resume VERSION
```

程序从草稿中读取原教材、章节和页范围，复用成功批次。提取已完成时继续审核修订阶段，不重新提取。仅需提取可加 `--extract-only`。

草稿和会话保存后端配置快照。**修改 teach.toml 中的模型、超时，或从 HTTP 切到 Codex 后，恢复时应显式传入 `--backend local_codex`**，才能更新已有绑定；只修改默认配置不会改变原快照。切换也不会重新生成已完成批次，若需要全新模型处理全部内容，应创建新草稿。

### 4. 自动审核修订与确认

`draft` 默认完成提取、程序校验、模型审核、按全部审核意见修订和再审核。整份草稿最多修订两轮，每个有效修订必须重新审核；提取批次的局部引用校正独立于这两轮。已有草稿或中断任务使用：

```powershell
teach --backend local_codex prepare VERSION
```

恢复会复用已保存阶段和候选，不重置轮数。修订成功后复审失败，只恢复复审。普通重复执行已完成或额度耗尽的任务不再次调用模型；显式使用 `teach --backend local_codex prepare VERSION --retry` 开启新的两轮尝试。

两轮后仅剩非阻断 warning 时，可确认学习；仍有程序错误或审核 error 时不能发布。知识错误、重要遗漏、题干没问却扣分和影响学习的依赖错误属于 error。模型检查不保证绝对正确，但不要求学习者替系统做专业校对。

准备完成后展示学习范围、主要目标、实际修改摘要和剩余提示。交互终端当场询问是否接受并开始，默认否；接受后自动确认、发布并进入教学。选择稍后时运行 `teach start VERSION` 再确认。非交互运行只展示结果，不自动发布。

提取、审核、修订、再审核、独立 `audit`/`revise` 及 review 内审核都有阶段提示；每 15 秒输出阶段等待和总耗时。进度写入 stderr，不估算模型生成百分比；成功、失败或中断时停止提示。输入超限会暂停并报告大小和配置位置，不截断原文或自动提高上限。

以下独立审核、修订和人工编辑方式仍保留，通常无需手动执行：

```powershell
teach audit VERSION
teach review VERSION
```

审核报告的原文引用必须逐字匹配对应文本块，且在块内唯一。校验失败时会显示问题编号、引用编号和具体原因，并将完整失败报告保存到本地事件记录；程序最多额外调用模型一次，只纠正错误引用，不改动审核意见或严重程度。纠正仍失败、超时或输入超限时暂停，保留已保存阶段；使用 `teach --backend local_codex prepare VERSION` 恢复。引用纠正不占用两轮教材修订额度。

审核同时看知识清单、教学顺序、原文引用、代表题和评分标准。`error` 必须经修订和再审核消除；`warning` 是非阻断建议。自动流程尝试处理两类意见。引言或导读的学习目标应以主题定位和关系为主，不擅自扩展成后续章节才提供依据的深入操作目标。

本次审核遇到的问题及处理方式：

| 问题 | 处理方式 |
| --- | --- |
| 重要主题漏出内容清单 | 补充原文支持的主题和目标映射，不补造具体内容 |
| 声称考核某种误区，但代表题允许持该误区的人得满分 | 增加能区分误区的题目要求，并纳入评分关键项；无依据时移除不适用关联 |
| 持续进化、多 Agent 协作被合成一个目标 | 拆成独立目标、代表题及评分，更新内容映射与前置关系 |
| 要求从模态、时序分析，但只考场景匹配 | 题目明确要求维度分析，评分覆盖对应维度 |
| MCP 登记为重要内容，但目标和题目不覆盖 | 补充原文支持的工具互操作定位及考核要求 |

需要模型协助修改时，先在审核提示符输入 `quit`，再回到 PowerShell 执行：

```powershell
teach revise VERSION --instruction "依据当前原文补齐遗漏主题，拆分混合目标；让代表题和评分真正检验目标及关联误区。保留未受影响的key，更新内容映射与前置关系，不超出原文范围。"
teach audit VERSION
teach review VERSION
```

`revise` 接收的是你提供的修改要求，并不会自动把上次审核意见全文作为输入。应把具体目标 key、问题和希望的改法写进 `--instruction`，上面的文字只是结构示例。复杂修改也可使用 `export/apply` 编辑 JSON，见[原型说明](docs/prototype.md)。

`resolve 编号 理由` **仅记录备注，不能放行 error，也不会修题或改评分**。任何草稿修改都会使旧准备状态、审核、处理意见和确认失效，须重新准备或审核。手动 `approve` 和 `publish` 也要求当前修订通过程序校验、完成审核且无 error。

自动准备和独立 `revise` 使用“完整输入、局部输出”：模型仍看到全部草稿与原文，但只返回需要改变的目标、内容映射、误解条目或备注。目标按完整对象替换，题干、答案与评分一起调整；未涉及的内容由程序原样保留。合并后仍校验完整草稿，自动准备继续整份复审。提取批次内的引用校正仍返回完整批次内容。

收到的修改包先保存再合并。`prepare` 中断后复用修改包；独立 `revise` 在合并或校验中断后，重复同一版本和相同指令可复用未处理的修改包。明确被拒绝的修改包不会在独立命令中反复复用。空包或无实际修改视为失败，自动流程消耗本轮额度，独立修订不增加版本号；不会悄悄退回整份草稿重写。

修订提示显示输入数据字符数（不含提示词与 Schema）、修改包字符数、完整草稿字符数和调用耗时；输出字符数按统一紧凑 JSON 统计。局部输出减少无关内容重新生成，但不保证固定速度提升。周期提示“仍在等待当前阶段完成”只说明客户端还在等待。

核对通过后，在审核提示符依次输入：

```text
approve
publish
```

### 5. 开始学习与恢复

```powershell
teach --backend local_codex start VERSION
```

回答默认支持多行：**回车换行，单独输入 `/end` 提交当前小问**。多小问题目逐问展示，前面的小问只保存、不评分，全部完成后统一反馈；整题仍算一次作答。反馈后回车继续。

```text
学习> 第一行回答
学习> 第二行回答
学习> /end
```

`/end` 必须单独一行输入；`不知道怎么写/end` 会作为回答正文保存，不会提交。最后一问提交后，程序先保存整题回答，再显示“回答已保存，正在评分，请稍候。”；等待期间每 15 秒提示耗时，结果校验并保存后显示“评分完成。”。`teach answer` 和恢复未完成评分也使用这些提示。评分失败时无需重答，按提示运行 `teach resume SESSION` 恢复。

生成下一题时也会显示进度。若模型生成的教材引用无效，程序记录失败候选并自动修正一次；仍失败则保留已有回答和评分，使用 `/continue` 重试生成。未通过校验的题目不会展示或计入作答次数。

`/cancel` 清空当前未提交输入，保留已提交小问；`/answer` 仍可使用，但无需先输入它。`/pause` 保存退出，EOF 或 Ctrl+C 也保留已接收的完整输入行和小问进度；尚未按回车的文字不保证保存。恢复会显示当前小问及已保存输入。整题最多 10000 字符，包含小问标记与换行；超限只拒绝新行，不清空已有输入。

新题使用结构化小问；旧题末尾清晰、连续编号的两到三小问可自动分步展示，不改原题和评分，含代码围栏或编号不明确时按整题展示。提示仍会使整题记为辅助作答；`/skip` 跳过整个目标，`/explain` 撤销当前题。正文以 `/` 开头时用 `//` 转义。`teach answer SESSION --text ...` 仍支持整题提交，但不能覆盖已有分步回答或输入缓冲。

下次打开终端，先设置好代理，然后：

```powershell
teach sessions
teach resume SESSION
```

会话默认沿用原后端快照；需要应用新模型或超时配置时，使用 `teach --backend local_codex resume SESSION`。`/ask 问题` 可跨章节检索当前教材，回答后保留原题及答案输入；`/explain` 只解释当前目标。

查看已经掌握和仍需学习的内容：运行 `teach progress VERSION`，或在学习中输入 `/progress`。列表按教材知识点标题分组，显示具体学习目标和中文状态，编号仅用于定位；顶部汇总已掌握、待复习、学习中、待补练和未验证数量。已练习不等于已掌握，“未验证”表示尚无有效评分证据。已排除目标会显示原因，不计入学习目标总数。列表覆盖整个版本，实际下一题由当前会话决定。

教学页面显示知识点标题、完整学习目标和最新掌握状态。**新作答由 AI 逐项判断固定核心要求：全部满足才通过；明确独立失败使有效达标次数减一，最低为零，历史通过不删除。** 两个不同场景的有效独立达标，且没有待补练缺口或已确认关键误解，才算掌握。新错误形成缺口，只能通过针对该缺口的独立补练解除。提示后的作答不增加独立次数，也不扣减。

核心证据不足时，系统最多追问两轮，每轮一个问题，继续用文字和 `/end` 提交；可以暂停恢复。补充回答属于同一次作答，最终只计一次。达到上限仍不明确，不算明确答错，也不扣次数。追问期间不显示纠正答案。

旧作答保留旧版数值评分和失败清零规则，复核也沿用原版本，不自动重写历史。新版反馈展示逐项能力判断与原因，不再用百分制决定通过。详见 [评分与学习进度规则](docs/grading-issues.md)。

模型返回“评分待复核”或“题目无效”时，该次结果不计入掌握证据，不能据此判断你没学会。进度列表会显示已作答、待复核、无效题和待评分数量；待复核记录附 `teach reassess ATTEMPT --reason "复核评分"` 命令。旧评分不自动改写，需复核的历史记录通过此命令新增评分修订后更新进度。

## 查找教材、草稿和会话

| 忘记的信息 | 查询命令 |
| --- | --- |
| 教材 ID（BOOK） | `teach books` |
| 章节 ID（SECTION）和页范围 | `teach outline BOOK` |
| 草稿/发布版本 ID（VERSION） | `teach drafts` |
| 某个版本的完整内容和审核意见 | `teach inspect VERSION` |
| 学习会话 ID（SESSION） | `teach sessions` |
| 作答 ID，供评分复核使用 | `teach attempts SESSION` |

删除不再需要的教材：

```powershell
teach books
teach delete-book BOOK
```

将 `BOOK` 替换为完整教材 ID。命令永久删除教材记录和数据目录内的 PDF 副本，保留原始导入文件。默认在教材有关联版本时拒绝删除，并列出版本 ID。

如果希望保留当前数据目录和其他教材，只清除这一本 PDF 的全部数据并重新开始，使用：

```powershell
teach books
teach delete-book BOOK --cascade
teach import "D:\教材\你的教材.pdf"
teach books
teach index BOOK
teach outline BOOK
teach learn BOOK --section SECTION
```

`--cascade` 会永久删除该教材的所有版本（包括已发布版本）、学习会话、作答及评分、全书知识结构、问答、自报掌握、检索索引、检查点和关联日志，并删除数据目录内的 PDF 副本；不可恢复。原始 PDF 和其他教材保留。重新导入后使用新的教材 ID，章节以 `outline` 输出为准。

自定义目录使用 `teach --data-dir PATH delete-book BOOK --cascade`，后续命令也须指定同一目录。如果记录删除后 PDF 清理失败，命令会报告残留文件路径供手动清理。

删除不再需要的未发布草稿（包括未完成提取的草稿）：

```powershell
teach drafts
teach delete VERSION
```

将 `VERSION` 替换为列表中的完整草稿 ID。删除立即生效且不可恢复，同时清理该草稿的提取批次、事件和后端绑定，保留教材 PDF 和其他版本。已发布版本不允许删除。使用自定义数据目录时运行 `teach --data-dir PATH delete VERSION`。

`teach drafts` 包括草稿和已发布版本，按创建时间从新到旧展示。现在以教材和章节名称为主，不再要求用户仅凭内部 ID 区分：

```text
教材：AI-Agents-in-Depth-zh-CN.pdf
章节：引言 · PDF 第 9–14 页
状态：草稿 · 第 1 次修订 · 待人工审核：2 项错误待处理，3 项建议
版本 ID：ver_...
下一步：teach review ver_...
```

复制实际输出中的完整 ID 或下一步命令；示例中的 `ver_...` 不能直接执行。下一步命令沿用绑定快照，若要改变配置，需要自己添加 `--backend`。

## 本次使用中遇到的问题与处理

| 现象 | 原因或当前判断 | 处理 |
| --- | --- | --- |
| 不理解为何从 `.venv\Scripts` 启动 | Windows 虚拟环境的标准命令入口目录，不是业务代码目录 | 激活环境后直接运行 `teach` |
| 以为默认 Codex，实际绑定 HTTP | 未创建 teach.toml，触发兼容 HTTP 路径 | 创建配置，并显式选择后端恢复已有草稿 |
| 修改 model 后仍用原模型 | 可能被 `#` 注释、未保存，或已有绑定仍用旧快照 | 检查配置并用 `--backend local_codex` 更新绑定 |
| 不知道 s1 是哪一章 | 章节 ID 按解析顺序分配，前言也可能占一个 ID | 提取前先运行 outline |
| Codex 调用超时 | 可能是代理连接等待，也可能是大批次生成耗时；仅凭超时无法确定 | 先检查同一终端代理，再调整执行时限并显式恢复；仍失败则看连接日志或新建较小页范围草稿 |
| `引用不唯一……c13_0` | 模型选取的短引用在同一原文片段重复出现 | 当前代码保存失败候选并自动校正一次，仍失败可 resume 继续修正；不跳过唯一性校验 |
| 学习时提示新题引用校验失败 | 下一题的引用未匹配本次提供的教材原文，或引用不唯一 | 自动修正一次；仍失败可 `/continue` 重新生成，已有回答和评分不受影响 |
| 审核出现 error/warning | 知识目标、题目、评分或覆盖存在问题，不是网络故障 | 按意见修改、重新 audit、人工确认后发布 |
| 忘记 VERSION、列表难辨认 | 内部 ID 不携带教材语义 | 使用新版 drafts，按教材名、章节和状态查找并复制命令 |

“引用不唯一”修复后的流程为：提取 → 校验 → 保存失败候选 → 按具体错误校正一次 → 再校验。校正失败不会进入成功批次或发布内容；下次恢复优先校正已保存候选。旧代码未保存的失败响应无法找回。每批最多一次业务校正调用，但该调用拥有独立的后端时限，因此整条 draft 命令可能超过单次 `timeout_seconds`，不应把 600 秒理解为整本教材处理上限。

## 使用 HTTP API 后端

在 teach.toml 的 `backends.remote_api` 中填写 `base_url`、`model`、`api_key_env`，密钥放在本地环境变量或显式环境文件中：

```powershell
teach --env-file .env.cli --backend remote_api draft BOOK --section SECTION
teach --env-file .env.cli --backend remote_api start VERSION
```

没有 teach.toml 时，仍可按 `.env.example` 使用原有 `TEACH_API_BASE/TEACH_MODEL/TEACH_API_KEY`。已有命名后端不会因传入环境文件就自动变成 HTTP，需显式选择。完整配置见[后端说明](docs/backends.md)。

导入只在本地解析，模型提取、审核、校正、出题和评分会发送相应材料给所选服务。默认数据目录 `data-v2/`，支持当前原型 schema 升级，不迁移已删除的旧系统数据库。使用全局参数 `--data-dir PATH` 时，各步骤都需指向相同目录。

## 安装与验证

新环境需要 Python 3.11 或更新版本：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,web]"
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check src tests
```

测试覆盖审核门禁、原文引用、先修关系、掌握判定、提示降权、评分修订、会话恢复、单写进程锁，以及真实 CLI 子进程的完整离线教学流程。真实模型的知识划分、题目质量和语义评分仍需实际教材验收。

## 文档

- [计划与进展](docs/plans/README.md)：当前优先任务、待验收事项、未排期改进和历史方案。
- [评分与学习进度问题讨论记录](docs/grading-issues.md)：实际误判案例、修复历史及已实现的新版评分和局部追问规则。
- [原型使用说明与实现边界](docs/prototype.md)：当前可执行命令、审核流程和设计取舍。
- [模型后端与认证](docs/backends.md)：Codex、HTTP API、已有登录、代理、后端切换与数据库迁移。
- [总体设计](docs/design.md)：目标架构和模块职责。
- [领域模型与教学状态机](docs/domain-and-policy.md)：证据、评分和教学策略。
- [各模块详细设计](docs/modules/README.md)：完整设计契约。

当前接口以原型使用说明和 `teach --help` 为准。支持本地只读 Web 检查；暂不支持 Web 写操作、OCR、向量数据库或多用户并发写入。
