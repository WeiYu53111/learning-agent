# 存储与运行配置详细设计

## 当前实现基线

当前 `storage.py` 使用 schema 3，表为 `books/versions/draft_batches/sessions/attempts/assessments/events/backend_bindings/preparations`，复杂对象保存为 JSON，掌握状态按证据重放计算。preparations 按 version_id 保存准备任务 ID、阶段、对应修订、已用轮数、修改包、候选、错误、调用计数及修改摘要。局部修改包返回后先保存为 merge 阶段，再合并和校验；不改变表结构，兼容旧任务中的完整候选。正式修订及复审结果与对应任务阶段在同一事务保存。独立修订的 revision_patch_received/processed 事件支持同版本同指令的中断恢复。下文第 1–5 节的 UnitOfWork、规范化表和索引是更完整的目标结构，不表示都已建表；第 6–7 节描述当前配置和迁移。

新增表：

```sql
CREATE TABLE backend_bindings(
  owner_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  spec TEXT NOT NULL
);
```

`owner_id` 为草稿版本或教学会话 ID；没有多态外键，由应用控制归属。`spec` 为经过严格 Pydantic 校验的 BackendSpec JSON，包括密钥环境变量名称，不含密钥值、登录凭据或代理地址。选择改变时同一事务更新绑定并追加 `backend_selected` 事件，包含名称、配置快照和前一个名称。该事务独立于后续模型结果提交，不能将选择事件视为调用成功。

`assessments.model` 保存 `后端名称/类型:模型标签`，原有修订不可覆盖。`batch_extracted/audit_backend/question_presented` 事件记录对应模型标签；不是完整逐调用账本。默认 Codex 模型未解析时明确使用 unknown 标签。旧版本的 model 字段保持原内容。

## 1. 边界和目录

目标存储层包含仓储、UnitOfWork、文件仓储和迁移，不决定教学动作或评分。以下为目标目录，当前尚无自动 backups/logs 管理；新原型 schema 1/2 → 3 的迁移不涉及已删除的旧系统数据库：

```text
data-v2/
  writer.lock
  learning.db
  documents/<sha256>.pdf
  staging/<operation-id>.part
  backups/
  logs/
```

路径以配置的 data_dir 为根解析。用户输入只用于读取源 PDF，内部写入名由程序生成，不使用上传名称拼接路径。启动时校验可写性；只读命令不初始化模型客户端。

同一规范化数据目录只允许一个写进程。所有写命令、初始化、迁移及交互会话先取得 writer.lock 上的操作系统排他锁，并持有至命令结束。锁覆盖模型等待和用户交互，但不等于持有数据库事务。第二写进程立即返回 OPERATION_BUSY；只读查询使用已提交快照，不争抢写锁。

锁适配器必须覆盖 Windows 和 POSIX，并归一化路径与符号链接/目录联接指向；不能仅凭锁文件存在或 PID 存活判定持锁。操作系统在进程结束时释放锁，保留的 writer.lock 文件不表示仍被占用。第一版仅支持本地文件系统，不提供网络共享目录的写锁保证。

## 2. 核心表与约束

所有时间为 UTC ISO 8601；ID 为 TEXT，布尔使用 0/1 CHECK，枚举使用 CHECK，JSON 列通过应用契约校验。所有外键启用，历史表默认 RESTRICT 删除。

| 表 | 主要字段 | 关键约束 |
|---|---|---|
| schema_migrations | version、checksum、applied_at | version 主键 |
| documents | id、sha256、original_name、relative_path、page_count、status | sha256 UNIQUE |
| parse_runs | id、document_id、parser_version、config_hash、status | 缓存键 UNIQUE |
| pages | id、parse_run_id、physical_page、raw_text、normalized_text、availability | (parse_run_id, physical_page) UNIQUE |
| knowledge_versions | id、document_id、parse_run_id、profile_hash、status、revision、coverage_json | 发布后不可修改 |
| sections | id、knowledge_version_id、parent_id、position、起止页与字符偏移 | 同版本父子关系 |
| source_chunks | id、knowledge_version_id、page_id、section_id、page_start/end、text、hash | 区间合法，单页 |
| knowledge_points | id、document_id | 稳定身份，不放可变正文 |
| point_versions | id、point_id、knowledge_version_id、section_id、title、description、quality | (point_id, knowledge_version_id) UNIQUE |
| objectives | id、point_version_id、dimension、description、required、position、rubric_json | id 标识目标内容版本 |
| objective_mappings | old_id、new_id、kind、reason | 仅记录，不自动继承掌握 |
| source_refs | id、owner_kind、owner_id、chunk_id、start/end、quote | 所有者及引用由仓储校验 |
| prerequisite_edges | source_objective_id、target_objective_id、kind、status | 三字段唯一、禁止自环 |
| misconception_definitions | code、knowledge_version_id、description、critical_targets_json | 受控误解目录 |
| sessions | id、learner_id、knowledge_version_id、lifecycle、phase、resume_phase、revision、current_target_id、active_task_id、needs_replan、updated_at | revision 非负 |
| session_targets | id、session_id、objective_id、origin、status、position、attempt_count、failure_count、replacement_count、unresolved_count、pending_verification | (session_id, objective_id) UNIQUE |
| remediation_frames | id、session_id、depth、parent_target_id、prerequisite_target_id、trigger_assessment_id | (session_id, depth) UNIQUE |
| teaching_decisions | id、session_id、target_id、action、reason_code、evidence_json、policy_version、context_hash | 追加保存 |
| teaching_tasks | id、session_id、decision_id、target_id、status、content_json | 活动任务唯一索引 |
| questions | id、task_id、objective_id、novelty_group、question_json、reference_answer、rubric_json、hints_json、targeted_misconception_codes_json | task_id UNIQUE，一任务至多一题 |
| exposure_events | id、session_id、question_id、kind、hint_level、operation_id、occurred_at | 操作内去重 |
| attempts | id、question_id、session_id、learner_id、answer、submitted_at、exposure_snapshot_json、current_assessment_id | question_id UNIQUE，原回答不可改 |
| assessment_revisions | id、attempt_id、revision、status、score、passed、result_json、misconception_results_json、supersedes_id、reason、rule/model/prompt_versions | (attempt_id, revision) UNIQUE |
| objective_mastery | learner_id、objective_id、score、status、state_json、rule_version、evidence_revision | 复合主键 |
| learner_projection_revisions | learner_id、revision | learner_id 主键 |
| misconception_states | learner_id、code、knowledge_version_id、status、evidence_json | 复合主键 |
| review_items | learner_id、objective_id、stage、due_at、status、last_assessment_id | 同目标一个当前安排 |
| operations | id、session_id、kind、input_hash、input_refs_json、status、execution_id、attempts_used、deadline_at、expected_revision、retry_of、result_ref、error_code | ID 唯一，session_id 可空 |
| session_events | id、session_id、sequence、operation_id、kind、payload_json、created_at | (session_id, sequence) UNIQUE |

SourceRef 的多态所有者不能仅靠 SQLite 外键保证，UnitOfWork 必须检查 owner 存在且知识版本相同；若实现中改为各对象专属引用关联表，保持相同领域约束。所有对象跨版本一致性均需仓储校验并配合适当复合外键，不能只依赖单个 ID 存在。

objectives.id 表示不可变目标内容版本，不等同维度或知识点稳定身份。版本间关系放 objective_mappings；会话、题目和掌握均引用确切 objective_id。

questions.targeted_misconception_codes_json 引用同知识版本的目录；assessment_revisions.misconception_results_json 保存 code、relation、rubric_keys、answer_spans 和 reason，仓储与契约共同检查引用。误解归约读取当前修订的完整结果，不能只保存支持列表而丢失反驳证据。

## 3. 数据库强约束与索引

至少实现以下约束和索引；具体建表 SQL 在实现迁移时编写并测试：

```sql
CREATE UNIQUE INDEX uq_active_task
ON teaching_tasks(session_id) WHERE status = 'active';

CREATE UNIQUE INDEX uq_running_session_operation
ON operations(session_id) WHERE status = 'running';

CREATE INDEX idx_review_due ON review_items(learner_id, status, due_at);
CREATE INDEX idx_sessions_resume ON sessions(learner_id, lifecycle, updated_at);
CREATE INDEX idx_attempt_history ON attempts(learner_id, submitted_at, id);
CREATE INDEX idx_edges_target ON prerequisite_edges(target_objective_id, kind);
```

operations 的 session_id 对导入批次可为空；同会话一个 running 操作。暂停/取消是对当前操作的控制事务，不需要先创建第二个 running 操作。新进程成功取得数据目录写锁后，将遗留 running 操作标为 failed_retryable(interrupted)，再决定恢复或新建显式重试操作，无需租约计时和抢占。

score 可空且非空时在 [0,1]，invalid_question/needs_review 的掌握计分为空。Attempt.current_assessment_id 必须指向自身修订；创建 Attempt 时允许为空。外键循环由同一事务内先插作答、再插修订、最后更新 current 指针解决。

## 4. UnitOfWork 与一致性

SQLite 连接配置启用 foreign_keys、WAL 和 busy_timeout=5000ms；事务短暂占锁。磁盘忙转换为可恢复错误，不能持锁等待网络请求。

持有数据目录写锁的进程使用 BEGIN IMMEDIATE 开启短事务，在事务内检查 session revision、操作 execution_id 及 running 状态。CAS 更新形如 `UPDATE sessions SET revision=revision+1 ... WHERE id=? AND revision=?`，影响行数为零即回滚整个事务。保留修订检查用于拒绝取消前的迟到结果与过时快照，不引入多进程投影冲突重试。

提交评估时，Assessment、current 指针、目标掌握、误解、复习、结果计数、反馈事件及会话阶段全部原子写入。原始 Attempt 已在先前短事务保存，即使后续失败也不丢失用户输入。保存 Attempt 时原子增加目标 attempt_count 并将活动任务改为 submitted；撤销改为 revoked，跳过改为 skipped，均在同一会话事务更新 active_task_id；评估后任务改为 assessed。这样不会遗留占用唯一活动索引的旧任务，重试也不重复增加总作答数。

单写进程串行更新学习者证据，所有追加评估和修订递增 evidence_revision，投影记录所用版本。历史修订无论新状态是否 valid，都在同一事务内切换 current 指针、重算掌握/误解/复习、标记相关未结束会话 needs_replan 并记录事件。结果为 unknown 时同步清除不再成立的复习项，不残留旧高分。

新评估候选尚未验证或重算失败时，不切换 current 指针。单次修订影响多目标时整体提交或整体回滚，不能一部分目标显示新分数、另一部分仍使用旧误解状态。

仓储按业务聚合提供 get_snapshot、append_attempt、commit_assessment、commit_task、cancel_operation 等接口；不得由 CLI 任意执行 SQL。只读查询可使用独立连接，模型调用期间释放连接。

## 5. 文件和发布一致性

导入先流式复制到 staging 并计算哈希，校验成功后原子移动到 documents 的内容寻址路径。同哈希已经存在则复用；所有导入串行持写锁，文档唯一约束额外防止重复入库。

文件移动与数据库事务不能假设为同一原子操作：先确保目标文件存在，再事务写入文档引用；失败留下未引用文件，由显式清理流程处理，不盲删可能被另一次导入引用的文件。损坏临时文件按操作归属回收。

发布只切换知识版本状态及发布记录，不移动或覆盖原 PDF。内容不可变性由写仓储校验和数据库触发器共同防护；修改必须派生草稿。

## 6. 配置契约

`BackendConfig` 根字段为 `active_backend` 和 `backends`，`BackendSpec` 禁止未知字段、要求严格类型及有限正数预算。配置文件默认是当前工作目录的 `teach.toml`；`--config PATH` 显式指定的文件不存在时报错。

| TOML 后端字段 | 默认或约束 |
| --- | --- |
| type | `codex_cli/openai_compatible/demo` |
| model | 默认空；HTTP 调用必填，Codex 空值使用内置默认模型 |
| executable / auth | `codex` / `existing_login`，用于 Codex |
| base_url / api_key_env | HTTP 地址 / `TEACH_API_KEY`，调用时读取变量值 |
| timeout_seconds | 180；HTTP 操作预算或 Codex 执行时限 |
| request_timeout_seconds | 90，仅 HTTP 使用 |
| input_chars | 48000，序列化任务数据和 schema 的字符预检 |
| output_tokens | 8000，仅 HTTP 使用 |

后端选择优先级不能简化为统一的“CLI > 环境”：

1. 已绑定的所有者且没有 `--backend`：沿用数据库配置快照，不读取新默认值。
2. 显式 `--backend`：从当前配置解析指定名称，并更新绑定、记录事件。
3. 新绑定：演示教材未显式选择时用离线后端；普通教材用 `active_backend`。
4. 无命名后端选择时：使用下表原有环境变量构建 `legacy_http`。命名 TOML 配置不会逐字段被 `TEACH_MODEL/TEACH_API_BASE` 覆盖。

| 配置项 | 默认或约束 |
|---|---|
| TEACH_DATA_DIR | `data-v2`，解析为绝对路径 |
| TEACH_MODEL / TEACH_API_BASE | legacy_http 的模型/地址，服务地址只允许 http/https |
| TEACH_API_KEY | 密钥，不写数据库和日志 |
| TEACH_REQUEST_TIMEOUT | 90 秒，正数 |
| TEACH_OPERATION_TIMEOUT | 180 秒，正数，请求时再受剩余预算限制 |
| TEACH_INPUT_CHARS / TEACH_OUTPUT_TOKENS | 48000 字符 / 8000 token |
| HTTP_PROXY / HTTPS_PROXY | 可选；Codex 继承进程环境，HTTPX 使用环境代理 |

数据目录优先级为 `--data-dir` > `TEACH_DATA_DIR` > `data-v2`。进程中已有环境变量优先于显式 `--env-file`，不自动读取 `.env`。teach 的 HTTP 适配器不隐式使用旧 OPENAI_API_KEY/DATABASE_PATH；Codex 认证仍服从它自己的机制。旧目标中的 TEACH_MAX_MODEL_REQUESTS、TEACH_CONTEXT_TOKENS、可配置 PDF 上限和日志等级尚未实现。

Codex 只检查已有登录，不负责账户管理；API 密钥、代理、Codex 身份不被绑定快照固定。恢复时读取当前环境/外部身份。`teach.toml` 作为不含凭据的项目默认配置纳入版本管理，默认 local_codex、gpt-6-astra、600 秒执行时限；配置结构的代码默认值仍见上表。`.env` 及 `.env.*` 被忽略，`.env.example` 保留提交。不得把密钥写入共享 TOML。

## 7. 迁移、备份与测试

首次写命令取得目录锁后初始化数据库。当前接受 user_version 0、1、2、3；空库先创建基础表，再增加 backend_bindings、preparations 并设置 PRAGMA user_version=3。版本 1/2 升级保留已有教材、会话和证据；重复初始化版本 3 不重复建表。其他版本拒绝，不自动降级。只读命令不迁移；空目录只读查询提示暂无数据。

旧草稿/会话没有绑定时，在首次需要后端时按当时选择建立快照，不根据旧 model 文本猜测服务地址或账户。需要明确来源时应显式指定后端。当前没有 schema_migrations/checksum 机制，也不自动生成升级前备份；这些仍为后续目标。后续备份应使用 SQLite backup API，避免复制活动 WAL 下不完整的单个 db 文件。

本次测试验证版本 1 数据保留、升级和重复初始化、绑定快照与显式切换、评分来源、密钥不进入快照，并回归原有证据及单写锁测试。完整迁移中断/磁盘故障、备份恢复、规范化表约束等仍需后续补齐，不列为本次已验收项。
