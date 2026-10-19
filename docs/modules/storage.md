# 存储与运行配置详细设计

## 1. 边界和目录

存储层实现仓储、UnitOfWork、文件仓储和迁移。不决定教学动作或评分。默认运行目录如下，旧数据库已经删除，不实现旧表兼容逻辑：

```text
data-v2/
  learning.db
  documents/<sha256>.pdf
  staging/<operation-id>.part
  backups/
  logs/
```

路径以配置的 data_dir 为根解析。用户输入只用于读取源 PDF，内部写入名由程序生成，不使用上传名称拼接路径。启动时校验可写性；只读命令不初始化模型客户端。

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
| sessions | id、learner_id、knowledge_version_id、lifecycle、phase、resume_phase、revision、current_target_id、active_task_id、updated_at | revision 非负 |
| session_targets | id、session_id、objective_id、origin、status、position、failure_count、replacement_count、unresolved_count | (session_id, objective_id) UNIQUE |
| remediation_frames | id、session_id、depth、parent_target_id、prerequisite_target_id、trigger_assessment_id | (session_id, depth) UNIQUE |
| teaching_decisions | id、session_id、target_id、action、reason_code、evidence_json、policy_version、context_hash | 追加保存 |
| teaching_tasks | id、session_id、decision_id、target_id、status、content_json | 活动任务唯一索引 |
| questions | id、task_id、objective_id、novelty_group、question_json、reference_answer、rubric_json、hints_json | task_id UNIQUE，一任务至多一题 |
| exposure_events | id、session_id、question_id、kind、hint_level、operation_id、occurred_at | 操作内去重 |
| attempts | id、question_id、session_id、learner_id、answer、submitted_at、exposure_snapshot_json、current_assessment_id | question_id UNIQUE，原回答不可改 |
| assessment_revisions | id、attempt_id、revision、status、score、passed、result_json、supersedes_id、rule/model/prompt_versions | (attempt_id, revision) UNIQUE |
| objective_mastery | learner_id、objective_id、score、status、state_json、rule_version、evidence_revision | 复合主键 |
| learner_projection_revisions | learner_id、revision | learner_id 主键 |
| misconception_states | learner_id、code、knowledge_version_id、status、evidence_json | 复合主键 |
| review_items | learner_id、objective_id、stage、due_at、status、last_assessment_id | 同目标一个当前安排 |
| operations | id、session_id、kind、input_hash、input_refs_json、status、worker_token、lease_until、attempts_used、deadline_at、expected_revision、retry_of、result_ref、error_code | ID 唯一，session_id 可空 |
| session_events | id、session_id、sequence、operation_id、kind、payload_json、created_at | (session_id, sequence) UNIQUE |

SourceRef 的多态所有者不能仅靠 SQLite 外键保证，UnitOfWork 必须检查 owner 存在且知识版本相同；若实现中改为各对象专属引用关联表，保持相同领域约束。所有对象跨版本一致性均需仓储校验并配合适当复合外键，不能只依赖单个 ID 存在。

objectives.id 表示不可变目标内容版本，不等同维度或知识点稳定身份。版本间关系放 objective_mappings；会话、题目和掌握均引用确切 objective_id。

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

operations 另含可空 session_id，导入批次操作可以为空；同会话一个 running 操作。暂停/取消是对当前操作的控制事务，不需要先创建第二个 running 操作。过期操作须先收回租约再领取，不能靠唯一索引过期后自动失效。

score 可空且非空时在 [0,1]，invalid_question/needs_review 的掌握计分为空。Attempt.current_assessment_id 必须指向自身修订；创建 Attempt 时允许为空。外键循环由同一事务内先插作答、再插修订、最后更新 current 指针解决。

## 4. UnitOfWork 与冲突

SQLite 连接配置启用 foreign_keys、WAL 和 busy_timeout=5000ms；事务短暂占锁。磁盘忙转换为可恢复错误，不能持锁等待网络请求。

写入前使用 BEGIN IMMEDIATE，在事务内检查 session revision、操作 token、lease_until 和 learner projection revision。CAS 更新形如 `UPDATE sessions SET revision=revision+1 ... WHERE id=? AND revision=?`，影响行数为零即回滚整个事务。

提交评估时，Assessment、current 指针、目标掌握、误解、复习、反馈事件及会话阶段全部原子写入。原始 Attempt 已在先前短事务保存，即使后续失败也不丢失用户输入。保存 Attempt 时将活动任务改为 submitted；撤销改为 revoked，跳过改为 skipped，均在同一会话事务更新 active_task_id；评估后任务改为 assessed。这样不会遗留占用唯一活动索引的旧任务。

跨会话学习者更新使用统一 evidence revision；重算时读取全部当前有效证据并在提交前验证该 revision。所有追加评估和修订必须递增它，防止漏掉并发证据。

仓储按业务聚合提供 get_snapshot、append_attempt、commit_assessment、commit_task、cancel_operation 等接口；不得由 CLI 任意执行 SQL。只读查询可使用独立连接，模型调用期间释放连接。

## 5. 文件和发布一致性

导入先流式复制到 staging 并计算哈希，校验成功后原子移动到 documents 的内容寻址路径。同哈希已经存在则复用；并发导入按文档唯一约束收敛。

文件移动与数据库事务不能假设为同一原子操作：先确保目标文件存在，再事务写入文档引用；失败留下未引用文件，由显式清理流程处理，不盲删可能被另一次导入引用的文件。损坏临时文件按操作归属回收。

发布只切换知识版本状态及发布记录，不移动或覆盖原 PDF。内容不可变性由写仓储校验和数据库触发器共同防护；修改必须派生草稿。

## 6. 配置契约

| 配置项 | 默认或约束 |
|---|---|
| TEACH_DATA_DIR | `data-v2`，解析为绝对路径 |
| TEACH_MODEL / TEACH_API_BASE | 模型任务必填，服务地址只允许 http/https |
| TEACH_API_KEY | 密钥，不写数据库和日志 |
| TEACH_REQUEST_TIMEOUT | 90 秒，正数 |
| TEACH_OPERATION_TIMEOUT | 180 秒，至少一个请求超时上限 |
| TEACH_MAX_MODEL_REQUESTS | 4，受固定格式/网络重试分类上限约束 |
| TEACH_CONTEXT_TOKENS / TEACH_OUTPUT_TOKENS | 对选定模型显式校验，不能盲用同一默认上限 |
| TEACH_MAX_PDF_MB / TEACH_MAX_PDF_PAGES | 20 / 2000，正数 |
| TEACH_LOG_LEVEL | INFO，受控枚举 |

优先级：CLI 显式参数 > 进程环境 > 显式指定的 dotenv 文件 > 默认值。第一版不自动加载保留的旧 `.env`，需要 `--env-file` 才读取；旧 OPENAI_API_KEY/DATABASE_PATH 不隐式继承为新数据配置。错误只输出配置项名，不输出密钥值。

## 7. 迁移、备份与测试

首次写命令在空目录建立 schema；只读命令遇到空目录报告暂无数据，不自动触发模型配置检查。schema 迁移编号、内容 checksum 固定，已执行迁移内容变化拒绝继续。

升级前用 SQLite backup API 生成一致性备份，不能直接复制仍在 WAL 写入的 db 文件。未知未来 schema 拒绝写入；第一版不实现自动降级。运行时数据目录整体加入 gitignore。

测试验证外键、活动任务唯一性、作答唯一性、修订 current 指针、CAS 回滚、WAL 恢复、租约过期、迁移中断、磁盘错误，以及文件已移动但文档事务失败时的可恢复性。
