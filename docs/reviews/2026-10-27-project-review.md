# 2026-10-27 项目复查与修复入口

用户授权先审查，再修复发现及既有待办，继续并发Luna、root统筹。开始版本为 `8184463`，fetch后Gitee/GitHub main与本地一致；产品源码仍承载前批受测 `cdf039a`。本轮隔离分支 `codex/review-remediation-2026-10-27`，未切换D工作区。以下是修复前审查，最终结果另记，不能把拟议测试当已通过。

## 审查方法与范围

三路Luna分别审查SQL输入/metadata/旧库边界、KB删除/上传/索引/恢复命令边界、启动鉴权/备份/维护CLI/前端契约；root核对实际调用链、共享连接与直接commit、历史待办及现有测试。审查以源码和现有合成测试为依据，不读取业务数据库/材料/.env正文，不使用真实模型或外部服务。这是本轮重点路径审查，不是所有执行路径均无缺陷的证明。

修复前六模块隔离基线 **44 passed、51.94秒、实际退出0**。覆盖test isolation、material upload commit、KB lifecycle API、backup restore/CLI和org audit。原始记录在 `artifacts/review-remediation-2026-10-27/baseline.*`。09:33首次核对8080无监听、ready不可达（测试前已如此），Dmain仍99834ca、跟踪树干净、配置哈希与此前一致；C无.env。不启动原服务，不修改旧迁移包。

## 确认需要修复的路径

| 编号 | 修复前位置 | 触发与影响 | 处理任务 |
|---|---|---|---|
| R1/P2 | `rag/materials/api.py` upload finalize之后的组织审计；archive confirm/recycle/restore的后置审计 | 审计失败可能在版本/job或业务状态已提交后导致HTTP失败；普通重传可新建版本/manifest，其他状态命令可能无法重试补审计 | T3a/T3b |
| R2/P2 | `rag/materials/archives.py:upload_zip` 与 `material_repo` 的manifest/entry直接commit | 归档多步持久化中后续失败会清quarantine却留下已提交记录；直接套外层事务不能阻止helper提前commit | T3a |
| R3/P2 | `rag/routers/knowledge.py:coordinate_kb_deletion` 审计早于finish_delete | 终态提交失败时已提交成功审计，重试可能重复；需将同库审计与终态SQL收拢，仍不承诺回滚已完成的文件/向量清理 | T1 |
| R4/P2 | organization confirm/rollback返回后逐项无条件审计 | conflict/未变更项被记成success；同key replay与已rolled_back项再次写审计。同KB多项产生不可区分的重复KB事件 | T3c |
| R5/P2 | `rag/backups/service.py:restore_test` rename后写报告 | 报告I/O失败留下正式目标，下一次restore-test被target_not_empty拒绝 | T2 |
| R6/P2 | `scripts/backup.py` restore-test保护根 | 外置本地Qdrant目录不在project/data内时未受保护，可能被当隔离恢复目的地 | T2 |
| R7/待复现 | `rag/tools.py:create_sql_tool` | 只读查询面使用默认sqlite3.connect，路径缺失时可能新建空数据库；应由行为RED确认，再使用文件只读打开，保留已有路径与memory兼容 | T5 |

R1/R2修复不能仅调整HTTP异常：必须验证业务SQL与审计共同提交/回滚，并保留原文件补偿。R3把审计放在cleanup之后会因policy已删除漏记，应在同一终态事务内先读取policy/写审计再执行cleanup。OrgRepository.locked_read使用独立reader；不得宣称所有词法嵌套读取都共享writer快照。

## 旧待办复核

剩余10条动态SQL记录暂未发现当前请求入口可触发的SQL缺陷：自定义迁移/目录、旧库catalog重放、显式runtime SELECT、生成artifact及手构workbook属于具体调用方信任合同。上一设计刻意保留合法quoted/非canonical对象及额外schema列；本轮不改成canonical白名单。补SQLite信任边界说明，PG可移植性仍未验收；不把10条follow-up伪称10个漏洞或通过修改计数完成迁移。

现有历史暂存恢复已覆盖CAS/receipt事务和commit失败保留原件/副本，未确认删除唯一可恢复源的新缺陷。API启动/鉴权与前端401/403静态审查暂未确认新问题，不重复开发已完成部分。

服务器真实矩阵/P99/SLO、真实脱敏fixture/供应商证据/业务质量、真实存量切换仍按既有安排留空；PG/UoW、共享状态、对象存储、多实例和生产shadow按依赖推进，不由本轮局部SQL事务修复冒称完成。

下一步：[设计](../superpowers/specs/2026-10-27-audit-and-restore-contracts-design.md)、[计划](../superpowers/plans/2026-10-27-review-remediation.md)。
