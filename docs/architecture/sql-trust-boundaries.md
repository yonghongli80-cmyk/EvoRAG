# SQLite SQL输入、artifact与旧库信任合同

本说明接续2026-10-26动态SQL源审查，逐项明确剩余10条`needs_followup`所处的SQLite边界。它描述当前支持范围，不把安全identifier引用等同ACL、不把source形状审查等同PostgreSQL兼容。最新源码/故障验证见[本轮复查](../reviews/2026-10-27-project-review.md)，源绑定清单须按最终冻结版本检查。

## 输入与执行者

| 输入面（对应旧10项） | 当前可信来源/执行范围 | 保证与限制 |
|---|---|---|
| `rag/db_migrations.py:run_migrations` 两处sink | UserDB启动默认使用源码中的版本/SQL脚本集合；自定义iterable是嵌入者提供的受信代码扩展 | 不是HTTP输入。调用者必须提供可信版本与脚本；不能将执行任意迁移的入口暴露给普通用户。SQLite executescript、user_version与duplicate-column升级策略不构成PG迁移协议 |
| `rag/db/migrator.py:Migrator` 文件脚本sink | legacy操作者API，读取显式选定或默认迁移目录的直属.sql文件 | 目录及文件等同可执行代码，受部署文件权限控制；不接受用户上传目录作为信任来源。该API不是当前UserDB启动runner；没有承诺不可信脚本沙箱或多语句全局原子性 |
| `rag/knowledge_tasks.py`与`rag/repositories/schema.py` 三处catalog重放 | 同一既有UserDB的sqlite_master/custom index/trigger SQL。knowledge_tasks迁移核对精确列序列；document_versions重建使用固定数据列清单，原实现不核对完整旧列集合，本轮T6补未知/生成列保护 | 支持受信本地旧库升级并保留兼容对象，不是任意外来SQLite的导入沙箱。不能将来源可信等同schema兼容；未知列不能被固定列复制静默丢弃。不可信旧库应在维护隔离与来源核定后迁移，不能因为来自catalog就视为安全。SQLite schema重放不可直接移植PG |
| `rag/spreadsheets/store.py` 两处sheet/column sink | 生产路径先经parser生成ParsedWorkbook；嵌入者也可手工构造dataclass | writer支持SQLite合法quoted/非canonical名称；统一quote helper转义双引号、拒绝NUL，值参数绑定。不新增canonical白名单，避免破坏前批明确保留的扩展兼容。手工构造者必须维护有效表结构/manifest一致性；不支持任意外来artifact作为业务资料 |
| `rag/spreadsheets/api.py` manifest-derived sink | 由应用生成/保存并通过当前授权/路径门禁取得的只读dataset manifest，request sheet_key按值绑定 | 请求不能直接替换SQL identifier；manifest对象名称经统一helper引用。artifact可信来源、document/version/ACL与文件身份是独立条件，quote并不能替代这些门禁。只读SQLite数据布局不是PG DAL |
| `rag/tools.py:create_sql_tool` runtime SELECT sink | 只有显式提供可读db_path的工具调用方才注册；db_path等同该完整数据库的读取授权 | 文件按mode=ro打开、缺失路径不建空库，保留:memory:。SELECT/分号/关键词门禁、query_only及1000行上限是额外约束，不是数据库行级/列级ACL，也不是不可信SQL的完整资源沙箱；不对任意递归/昂贵SELECT承诺超时 |

数量对应2+1+3+2+1+1=10。合同说明使当前SQLite支持边界可追溯，不强行把`needs_followup`改成`understood_shape`或宣布迁移ready；后续PG/不可信导入/资源配额策略仍需专门设计和验收。

## 只读暂存审计快照

`rag/staging_audit.py`使用显式snapshot路径，关闭原SQLite复制连接后按只读/immutable/query_only打开副本，只查询固定core/optional表；额外schema列来自PRAGMA并经相同identifier helper引用。额外列、含引号列和历史扩展是兼容要求，不能只保留canonical列。调用方必须提供有来源记录、已经静止的独立快照；本工具不证明外部写者已冻结，也不授权把业务运行库假作immutable副本。

审计CLI是inspect-only。历史暂存恢复仍要求独立RecoveryPlan、actor/ACL、文件归属/链接安全/hash/状态CAS与同事务receipt。复制原件、版本指针、归档身份与引用检查不能被metadata信任说明替代。

## 下一阶段的输入

在此边界之上继续建立完整writer/command ownership，验证故障、重试和未知commit结果，再选择UoW/PG adapter、持久幂等/outbox与多实例fencing方案。静态扫描候选数、10条待定输入合同或局部事务收拢都不证明所有writer已覆盖；服务器容量、业务质量与真实存量切换证据继续分别管理。
