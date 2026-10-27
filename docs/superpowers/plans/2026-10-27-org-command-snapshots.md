# Organization Command Snapshots Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox syntax for tracking.

**Goal:** 修复组织管理SQL/audit提交与组织授权单次读取的快照边界，补对应writer所有权证据。
**Architecture:** 四命令沿用原transaction并嵌入audit；授权服务每个公开读调用借助独立只读快照和局部绑定repo。三路并行、root串行测试和独立复审。
**Tech Stack:** Python3.12、sqlite3 WAL、pytest、标准库。
**Spec:** docs/superpowers/specs/2026-10-27-org-command-snapshots-design.md

## Global Constraints
- 仅修改C隔离worktree；不切换D目录、不读取.env正文或业务库、不调用真实模型、不开启真实服务/正式压测。
- Python3.12与现有依赖锁，不新增依赖、不做schema迁移，不改变403/404/admin/legacy KB政策和现有嵌套transaction语义。
- 6.1 Terra/high三路文件范围互斥；实现代理不自行提交、切分支、推送、pytest或创建子代理，root串行调度测试并实际独立复审。
- RED_READY即源码/测试冻结点；后续修改先由root排程，防止正在运行的测试加载不同版本。
- 所有SQLite/文件fixture在新OS临时目录，离线网络护栏、低优先级和BLAS单线程。
- 不把读快照当作完整授权writer准入、跨进程fencing、PG或多实例证明；10项SQL输入follow-up和服务器/真实业务验收继续保留。
- 本批默认仅本地验收；已双推的ce6da1b及旧补充包不受本批未验代码影响。

## Review Focus
1. 成功audit语句或deferred commit失败时，四命令的业务状态、安全版本和audit均不得部分提交，重试不得遗留重复grant。
2. 敏感授权需要scope和qualification同时存在，不能拼接一次撤权前后的两种各自拒绝状态变成允许。
3. 集合与展示读取（含role/metadata/document ids）必须与决定共享快照，不在子调用重新开新视图。
4. 共享service实例并发调用必须使用各自thread-local连接，快照异常不得污染下一次决定或回滚调用者已有reader事务。
5. 代码审计图必须绑定最终源码并说明scope，不能用词法嵌套证明完整writer或跨存储原子。

## Task 1: 四个组织管理命令audit原子性
**Files:** Modify rag/org_auth/api.py; Create tests/test_org_admin_audit_atomicity.py. 不修改OrgAuditService/Repository/Service。
**Interfaces:** assign_knowledge_base_policy、update_person、create_grant、revoke_grant原签名/响应/错误码不变；OrgAuditService.record既有嵌套参与。
- [x] 跟踪四入口SQL、security_version和audit调用，记录实际提交owner。
- [x] 先写参数化真实SQLite trigger语句失败和deferred FK commit失败测试，覆盖四入口；断言原policy/person/grant、安全版本、audit保持，合法重试只提交一次本次变化。
- [x] RED_READY发root并冻结；实际RED后才最小移动audit到原transaction内部，不改变无关权限/幂等语义。
- [x] root运行 tests/test_org_admin_audit_atomicity.py tests/test_org_admin_api.py tests/test_org_audit.py tests/test_org_identity.py tests/test_org_kb_guard.py。
- [x] 写task-1-report，独立SPEC/QUALITY审查后root分项提交。

## Task 2: 组织授权一次公开调用的读取快照
**Files:** Modify rag/org_auth/service.py, rag/org_auth/repository.py; Create rag/org_auth/read_snapshot.py if needed; Create tests/test_org_authorization_snapshot.py. 不修改api.py、audit.py、UserDB或ConnectionManager。
**Interfaces:** 六公开方法签名/返回保持；OrgRepository可选keyword-only read_connection绑定只读查询。私有authorization_read_snapshot(db) yield (conn, bound_repo)，新建短读transaction，外部已有reader事务则只借用。service实例不存调用态连接。
- [x] 先写合成敏感库并发撤权反例：初始member=true/qualified=false，另一连接原子切到member=false/qualified=true；控制在两次读取间提交，任何单一快照都拒绝，当前混合结果不能允许。
- [x] 补allowed_kb_ids、allowed_document_ids及visible_knowledge_bases的集合/metadata/role一致性，保留管理员、legacy/unassigned、deny优先级现有结果。
- [x] 补共享service跨线程、reader外层事务借用、异常释放后下一次调用与WAL外部writer可提交；用事件/回调而非sleep。
- [x] RED_READY发root并冻结；实际RED后实现局部conn/repo读取与内部helper复用，全部user/person/policy/grants/qualification/metadata取同一连接。
- [x] root运行 tests/test_org_authorization_snapshot.py tests/test_org_policy.py tests/test_org_identity.py tests/test_org_acceptance.py tests/test_org_retrieval_scope.py tests/test_review_authorization_boundaries.py tests/test_org_admin_api.py。
- [x] 写task-2-report，独立SPEC/QUALITY审查后root分项提交。

## Task 3: 组织命令writer所有权与源绑定图
**Files:** Create docs/architecture/organization-command-ownership.md; Create plan workspace org-command-source-map.json. 只读其他代理产品，不修改其文件或共享状态文档。
**Interfaces:** 图覆盖Task1四入口+Task2六读入口及OrgAuditService/ConnectionManager实际边界；map含commands、evidence path#Lline、source_sha256、coverage=scoped_only、writer_completeness=not_proven。
- [x] 先从基线ce6da1b追踪四写命令及六读入口，列实际conn、begin/commit owner、helper、audit、安全版本影响和SQL以外效果；稿件标注draft，不把未来改动当已完成。
- [x] 为每项列出已验故障切点和仍缺故障/authorization writer admission；不声称处理1707个全部writer。
- [x] 等root通知Task1/2实现冻结，再根据真实最终源码更新每个函数/行/哈希及文档；不在他人编辑时封最终hash。
- [x] root用纯AST/文件读取检查所有源引用合法、哈希新鲜和精确六/四入口集合，再独立事实复核。
- [x] 写task-3-report，完成可独立拒绝的源绑定所有权图；不执行应用或SQL，不跑pytest。

## Task 4: root集成、冻结与本批记录
**Files:** Update docs/local-readiness.md, docs/README.md; Create docs/reviews/2026-10-27-org-command-snapshots.md; new artifacts/org-command-snapshots-2026-10-27.
- 本轮完整回归额外允许只修 tests/test_material_visibility_batch_plan.py 的连接计数代理：补齐真实SQLite连接的in_transaction/rollback委托，保留所有ACL/行集合/查询数断言，不改变生产快照逻辑。首轮失败及源码核对保留，修后重新冻结验证。
- [x] 新计划独立ledger/brief和前后原目录基线，root串行跑RED/GREEN，禁止实现代理ready后私自编辑。
- [x] 三任务独立审查，消除重要问题；全树Ruff、变更模块必要格式及既有组织/材料授权集成回归。
- [x] 代码停止修改后提交冻结，跑完整后端与生产锁烟测，官方源清单前后校验，按新源码重建SQL/writer清单和受影响SQL来源审查。
- [x] 整批独立复审与单次修复波，文档区分本轮结果、已交付main、仍待服务器/完整writer准入。
- [x] 实际提交本事项公司记录并核对成功回执；保留本地待审成果，不自动推送新批或更新补充包。

