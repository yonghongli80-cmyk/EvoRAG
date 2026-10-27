# 组织命令与授权读取所有权图

源状态：`final_working_tree_before_commit`，基于 `ce6da1b` 的未提交工作树；不是基线源码。机器可读清单 `.superpowers/sdd/2026-10-27-org-command-snapshots/org-command-source-map.json` 将入口和辅助函数绑定到仓库相对路径、定义行及文件原始字节 SHA-256。覆盖范围仅四写、六读：`coverage=scoped_only`，`writer_completeness=not_proven`。

## 写事务及审计所有权

四个入口均经 `UserDB.locked_transaction`（`rag/user_db.py#L98`）进入 `ConnectionManager.transaction`（`rag/db/connection.py#L139`）。后者以进程内 RLock 保护共享 raw writer；没有外层事务时执行 BEGIN 或 BEGIN IMMEDIATE，成功后 commit，异常时若仍在事务则 rollback。已有事务时仅 yield 相同 writer，不负责 BEGIN、commit 或 rollback，由已有外层事务的所有者决定最终结果。四个入口均在该事务块内调用 `OrgAuditService.record`（`rag/org_auth/audit.py#L12`），该方法再次进入同一个管理器；由于 RLock 可重入且 writer 已在事务中，嵌套调用借用同一连接且不独立提交。审计插入失败会沿调用栈传播到拥有事务的一层；真实 commit 失败也由拥有事务的管理器回滚（前提是 SQLite 仍报告事务中）。因此这里的结论依赖实际连接和管理器分支，而非仅凭源码缩进。

| 入口和证据 | 同一 writer 的操作与 security_version | 外部可见效果 |
|---|---|---|
| `assign_knowledge_base_policy` `rag/org_auth/api.py#L34` | 默认 BEGIN；验证 KB、活动组织和敏感域，插入或更新 policy，插入 `kb_policy_assigned`；不递增 security_version | 返回 policy；失败传播，未引入请求幂等协议 |
| `update_person` `rag/org_auth/api.py#L209` | BEGIN IMMEDIATE；人员字段、membership、manager scope、qualification 与 `person_updated`；active、membership、scope、qualification 各自触发一次版本递增，job_title 不递增 | 返回 updated；空 body 仍调用审计，不因无变更自动取消 |
| `create_grant` `rag/org_auth/api.py#L304` | 默认 BEGIN；校验活动 policy 和主体、插入 grant、`_invalidate_grant_subject`（`rag/org_auth/api.py#L284`）递增用户或组织子树成员的版本，插入 `grant_created` | 返回新 id；无请求幂等协议 |
| `revoke_grant` `rag/org_auth/api.py#L337` | 默认 BEGIN；读取活动 grant、置 inactive、同 helper 递增主体版本、插入 `grant_revoked` | 返回 revoked；重复撤销可能 404 |

四者先进行公司管理员检查；函数内没有外部服务或文件调用。返回和异常属于 SQL 外可见效果；HTTP 请求是否被重试、其他调用者的额外副作用不在此图范围。成功审计只覆盖本次成功路径，不代表拒绝和所有失败都产生 audit。

## 授权读快照所有权

六个公开入口各调用 `authorization_read_snapshot`（`rag/org_auth/read_snapshot.py#L7`），通过 `locked_read`（`rag/user_db.py#L103`）和 `ConnectionManager.get_read_connection`（`rag/db/connection.py#L73`）取得线程本地 query_only reader，而非共享 raw writer。若 reader 没有外层事务，helper 执行 `BEGIN`，首次 SQL SELECT 固定 SQLite 读视图，并在正常或异常退出时以 rollback 释放自己拥有的事务；若已有事务则借用，不结束外层事务。它创建只在调用中传递的 `OrgRepository(db, read_connection=conn)`，`_read`（`rag/org_auth/repository.py#L23`）使用绑定连接。无绑定的普通 repository 读取依旧分别调用 `locked_read`，不自动获得此调用的快照。共享 service 的 `self.repo` 不保存此次调用的连接。

| 入口和证据 | 同一 reader 的内部路径和结果 |
|---|---|
| `decision` `rag/org_auth/service.py#L14` | `_decision` 读取 users、person、policy、membership、树、manager scope、grant、unit、qualification，计算角色 |
| `can_manage_org` `rag/org_auth/service.py#L46` | `_can_manage_org` 读取 user，管理员分支取 unit，其他分支取 person、scope 与 descendants |
| `authorize` `rag/org_auth/service.py#L59` | `_authorize` 调 `_decision`，再比较 required role |
| `allowed_kb_ids` `rag/org_auth/service.py#L69` | `_allowed_kb_ids` 读 policy IDs，逐项 `_authorize` |
| `allowed_document_ids` `rag/org_auth/service.py#L77` | `_authorize` 成功后同连接读取未删除 document_permissions IDs |
| `visible_knowledge_bases` `rag/org_auth/service.py#L87` | `_visible_knowledge_bases` 同连接计算允许集合、读 metadata/组织/文档数，逐项 `_decision` 得角色 |

用户投影在 `rag/org_auth/repository.py#L28` 包含 id、username、bool is_admin、password_changed_at。六入口本身不写 audit 或 security_version；它们的可见效果是返回结果或异常。单次快照可以是旧视图，不能据此推断它之后的写入仍被授权。

## 验证及剩余边界

Root 报告冻结前核心 GREEN 为 48 passed（114.25 秒，native exit 0）；之后仅 service import 排序修正，全树 Ruff 通过，两个新测试及 helper 格式调整经 AST 等价检查。此报告引用该结果，并不声称本任务代理自行执行了测试。`tests/test_org_admin_audit_atomicity.py#L35` 参数化四入口和审计 INSERT 语句失败、延迟外键导致真实 commit 失败，检查 policy/person/grant/audit 状态及成功重试；所选 person 情景为 active/job_title，grant 情景为用户主体，未穷举 membership、组织主体或其他故障。`tests/test_org_authorization_snapshot.py#L56` 覆盖五个入口的敏感库并发切换，另有 metadata/role、document IDs、六入口合同、已有 reader 事务、异常释放、共享 service 跨线程及 WAL 写入切点；`can_manage_org` 有合同测试但没有单独的并发切点测试。测试来源和精确行号另列于 JSON。

仍未证明鉴权之后直至写入的 writer admission 或 fencing、所有组织相关 writer 的完整性、跨进程协调、PostgreSQL 或多实例行为。其他故障、不同外层事务、外部系统和所有安全版本组合未穷举；10 项 SQL 输入 follow-up、服务器及真实业务验收继续保留。最终映射和事实仍须 root 独立核验。
