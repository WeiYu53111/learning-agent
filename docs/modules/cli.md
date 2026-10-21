# CLI 模块详细设计

## 1. 职责与依赖

CLI 解析命令、采集回答、渲染公开视图、处理终端信号。当前通过 Application 执行业务，并使用 Store 做部分列表查询，构造 BackendRegistry 注入应用；不自行计算掌握或处理供应商认证。全面使用类型化 Facade/ViewModel 是后续目标。

初始命令解析使用标准库 argparse；终端输出先采用普通文本，颜色和富文本为可选增强。非 TTY 输出不使用动画或控制字符。中文路径和空格路径遵循 shell 引号规则。

## 2. 顶层命令

| 命令 | 输入与行为 |
|---|---|
| `teach import PATH` | 只在本地解析和保存 PDF，相同文件复用，不调用模型 |
| `teach books` / `teach drafts` | 分别列出教材和内容版本 |
| `teach delete-book BOOK` | 永久删除无关联版本的教材及 PDF 副本；有关联版本时拒绝并列出 ID，保留原始导入文件 |
| `teach outline BOOK` | 查看章节 ID 和物理页范围 |
| `teach draft BOOK --section SECTION [--pages START:END]` | 自动提取、审核、最多两轮修订及复审；失败用 `draft --resume VERSION`，`--extract-only` 仅提取 |
| `teach prepare VERSION [--retry]` | 准备已有草稿或恢复任务；--retry 显式开启新一轮任务 |
| `teach delete VERSION` | 永久删除未发布草稿及其提取批次、事件和后端绑定；拒绝删除已发布版本 |
| `teach inspect VERSION` | 展示草稿质量问题、引用与待确认项 |
| `teach audit VERSION` / `teach review VERSION` | 模型审核 / 人工交互审核校正 |
| `teach approve VERSION` / `teach publish VERSION` | 确认当前审核修订 / 发布已确认版本 |
| `teach start VERSION [--point POINT] [--due] [--no-chat]` | 使用已发布版本进入教学；复习用 --due |
| `teach resume [SESSION] [--no-chat]` | 恢复指定会话；仅一个可恢复会话时可省略 ID |
| `teach progress VERSION` | 目标级状态、独立通过次数和复习安排 |
| `teach attempts SESSION` | 原始作答、最新评分修订及评分后端/模型 |
| `teach reassess ATTEMPT --reason TEXT` | 显式复核原作答，创建新评估修订；不要求用户再答同题 |

当前 import 不生成草稿，publish 不提供 `--yes`；发布门禁是当前修订通过程序校验、经过 audit 且无 error，再经人工确认。resolve 备注不能放行 error。完整命令含 export/apply/revise/resolve/clone/answer/next，见[命令清单](../prototype.md#6-当前命令清单)。`teach ask` 与 `/ask` 尚未实现。

自动准备完成后，交互终端当场展示学习范围、实际修改及剩余建议并询问是否开始，默认否。确认后发布并进入教学；拒绝或 EOF 保留结果。非交互执行不发布。`start VERSION` 可对准备完成的草稿重新请求确认；对已发布版本保持原行为。

全局参数 `--config PATH`、`--backend NAME`、`--env-file PATH`、`--data-dir PATH` 放在子命令之前。CLI 仅把配置路径和显式选择传给 BackendRegistry；绑定优先级由应用执行。start/resume 展示所选后端、类型和配置模型，默认值明确显示“后端默认”。

`draft`、`prepare`、独立 `audit`/`revise` 及 review 内的审核将阶段进度实时输出到 stderr：批次及 PDF 页范围、后端/模型、缓存复用、提取、审核、修订轮数、等待局部修订、收到修改包、合并、完整校验、复审和保存。每 15 秒输出当前阶段及总耗时，表示客户端仍在等待，不代表模型生成百分比。修订还显示输入数据、修改包、完整草稿字符数和调用耗时；这些不是 token 数。完成、异常或中断时停止计时提示，非 TTY 同样使用普通文本行。

`drafts` 面向人工选择，按创建时间从新到旧，以教材文件名、章节标题和实际选定 PDF 页范围为首要信息。状态使用中文，区分未完成提取、待模型审核、待人工审核、已确认待发布和已发布；审核摘要显示尚未处理的 error 数及 warning 数。每条保留完整版本 ID，并给出下一步可复制命令，演示教材单独标注。列表不调用模型，也不改变审核或发布状态。

Codex 需要代理时，在启动 teach 的同一终端设置 `HTTP_PROXY/HTTPS_PROXY`，或使用显式环境文件。CLI 不代为登录、不回显提供方完整错误；认证失效提示用户在 Codex 中修正后恢复。选择后端错误不自动换服务。配置示例见[后端说明](../backends.md)。

最近会话按 updated_at 降序选择 active/paused。多个未结束会话先列候选，要求指定 ID；completed/ended_with_gaps 默认只读展示，需要继续学习则开启新会话。

## 3. 交互输入状态

| 输入状态 | 普通文本 | 可用操作 |
|---|---|---|
| awaiting_answer | 提交当前题；空白输入不提交 | hint/explain/skip/progress/why/pause/help |
| feedback_ready | 仅 `继续`、空行或 `/continue` 确认推进；其他文本给出帮助 | explain/progress/why/skip/pause/help |
| planning 或模型运行中 | 应用服务自动推进，不要求用户输入解锁，也不缓冲下一题答案 | Ctrl+C |
| 多行编辑 | 累积文本 | `/end` 提交，`/cancel` 丢弃编辑 |
| 终态 | 不自动再开题 | 查看总结后退出 |

首次独立通过后，“继续”触发不同场景的新题以补足证据。ADVANCE 可连续跳过已经达标的目标，但必须在同一次用例中返回下一题、纯讲解、暂停或总结；CLI 不展示一个永久等待的 planning 提示符。内部规划无进展时展示 POLICY_STALLED 及已保存检查点。

达到作答上限的目标以 feedback_ready 展示 deferred 原因，允许 `/explain` 仅展示讲解、`/skip` 或 `/quit`；不会再出评分题。其他 feedback_ready 阶段的 `/explain` 同样只针对当前目标讲解，尚有预算时用户下一次确认可进入验证。

输入行以 `/` 开头时先解析命令；`//` 转义为回答中的一个 `/`。多行编辑中除 `/end`、`/cancel` 外均视为正文。一次回答最多 10000 字符；达到上限时允许编辑或取消，不静默截断。空白答案无效。

`/skip` 跳过整个当前目标，不只是某一道题，执行前展示范围。`/explain` 撤销当前活动题并记录答案暴露，再展示讲解；后续独立验证必须换题。`/hint` 用尽后明确提示无更多提示，不重复记录新使用次数。

## 4. 展示契约

ApplicationFacade 返回公开 ViewModel，而非完整领域对象：

- SessionView：生命周期、阶段、目标、任务、可用命令、操作状态。
- TaskView：动作、目标描述、讲解、例子、公开题干、来源页码。
- FeedbackView：有效性、覆盖/缺失、解释、掌握状态变化、下一步建议。
- ProgressView：required 目标数、达标、待复习、跳过、阻塞、尚未验证。
- ImportView：草稿、覆盖率、硬错误、警告、可发布状态。

TaskView 不含 reference_answer、rubric 和尚未请求的提示内容。反馈可展示参考解释，但不能把历史题复述计为新证据。

每题展示目标、问题和页码；长讲解分段输出，不主动清屏。`/why` 展示简短决策理由与证据 ID，不展示模型隐式推理。模型与磁盘错误只展示安全消息，debug 模式也不输出敏感正文。

## 5. 操作标识与恢复

用户明确提交一次输入时生成 operation_id；网络重试与恢复沿用原 ID。命令重发不是自动认定为相同操作，应用层仍按题目唯一作答约束阻止重复计分。

提交前 CLI 保留输入缓冲。应用确认原始作答已落库后显示“回答已保存，正在评估”。若持久化失败，保留本次终端缓冲并允许重试，不声称已保存。

Ctrl+C 在编辑阶段丢弃未提交缓冲并暂停；在模型运行中先使操作取消标记生效，再暂停，迟到结果不得提交。EOF 保存已提交状态后退出。磁盘故障无法保存时必须明确告知；不可用退出消息掩盖失败。

## 6. 退出码与测试

当前 argparse 参数错误及配置文件不存在返回 2；捕获的模型/业务/配置/存储错误返回 1，正常结束返回 0，顶层 Ctrl+C 返回 130。交互内可处理错误可能在返回提示符或保存退出后以 0 结束。下表为尚未实现的更细分类目标，不能用于当前脚本判断错误类型。

| 退出码 | 含义 |
|---|---|
| 0 | 正常结束、保存成功的暂停或用户拒绝发布 |
| 2 | 参数、命令或输入错误 |
| 3 | 模型或外部服务失败，可恢复 |
| 4 | 数据、配置、schema 或存储故障 |
| 5 | 并发冲突或其他进程占用 |
| 6 | 内部规划无进展，已暂停会话 |
| 130 | Ctrl+C 结束，保存结果另行明确展示 |

使用假 Facade 验证解析、状态允许命令、公开字段隐藏、中文路径、非 TTY、多行转义、重复提交、EOF 和取消；不为纯文案编写快照测试。真实终端信号使用子进程集成测试。

写命令启动后持有数据目录排他锁直到退出；另一终端的写命令立即显示“该数据目录正在使用”，退出码 5，不等待租约。books/outline/progress 等只读命令仍可读取已提交数据。持锁进程崩溃后用户可直接 resume，由操作系统释放锁，不要求手动删除锁文件。
