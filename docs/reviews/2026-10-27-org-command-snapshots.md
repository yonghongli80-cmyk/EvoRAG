# 2026-10-27 组织管理提交与授权快照

## 范围与状态

用户要求先合并双推上一批，再继续并发6.1 Terra推进本地待办。上一批已快进交付两端main至ce6da1b，标签一致，无强推；本批从该提交在C隔离分支codex/org-command-snapshots-2026-10-27继续。原D目录main保持99834ca，服务器验收保留，旧补充包保持8eac1fa，不因本批未验改动更新。

本页记录本批阶段结果，完整后端/生产锁及源冻结后验证尚待收尾；不能拿上一批3572/5作为新批结果。见[设计](../superpowers/specs/2026-10-27-org-command-snapshots-design.md)、[计划](../superpowers/plans/2026-10-27-org-command-snapshots.md)和[作用域所有权图](../architecture/organization-command-ownership.md)。

## 已实现

- assign_knowledge_base_policy、update_person、create_grant、revoke_grant的成功audit加入原业务transaction；与policy/person/grant/security_version共同提交或回滚，保留原HTTP/admin/no-op和安全版本策略，不添加全局请求幂等。
- OrgAuthorizationService的六个公开方法在单次调用及其嵌套决策、集合和metadata读取中共享同一thread-local只读SQLite快照。创建的reader事务在退出时释放，已有外层reader事务只借用；不获取writer锁、不存共享调用态连接、不更改默认repository独立读合同。
- 四写六读所有权图绑定10入口、34个helper/test定义行和原始SHA，root纯AST/文件验证44引用通过；scope为scoped_only，writer_completeness仍not_proven。

授权快照固定于第一次SQL读取，允许稳定旧视图或新视图。它不保证响应发出时仍是最新权限，也不把鉴权和后续业务写入变为共同writer snapshot。完整writer admission、跨进程fencing、UoW/PG与多实例仍未完成。

## 实际分项证据

既有组织路径基线21通过/122.80秒/native0。新增RED为16失败/3通过、29.03秒/native1：四命令8个语句/deferred commit切点确认业务已提交而审计失败；8个读取反例确认敏感scope/qualification拼接允许、metadata/role与document id视图不一致及跨线程旧视图错误。

修复后联合48通过/114.25秒/native0，覆盖新19项和既有组织admin/audit/identity/kb guard/policy/acceptance/retrieval及授权边界。其后只修service导入排序，三个新文件格式调整均有AST等价记录；全后端Ruff通过。两个实现任务与所有权图均经独立SPEC/QUALITY通过，整批静态复审未发现本批P1/P2/P3可操作问题；这些不替代最终冻结版本的完整运行验收。

原始日志、JUnit、source map校验及过程脚本保留在artifacts/org-command-snapshots-2026-10-27；本计划独立SDD目录保留源码快照和审查报告。过程脚本包含本机环境路径，不作为新机直接运行入口。

## 保留的待办

服务器资源门禁/正式矩阵/P99/SLO、真实业务迁移恢复、真实脱敏fixture/供应商证据、Linux专项继续未验。其他业务命令writer图和故障矩阵、授权writer admission、10项SQL输入follow-up、未知commit结果、PG/UoW、共享状态/outbox/多实例按既有依赖继续，不由本批局部修复冒称完成。
