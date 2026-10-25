# 暂存审计 jobs 关联索引设计

用户要求先计划，再由主代理统筹 Luna 并行执行，保护现有服务与业务数据。基线为`9a8653c1e4fc51d462268aa8c9fb54403a972720`；本轮只优化显式快照审计，不扩展到KB删除或恢复写流程。

## 问题与选择

`rag/staging_audit.py`已按id一次读取并校验jobs，但`_diagnose_version`为每个版本重扫全部jobs，关联成本为Θ(V·J)。此前100/500/1000规模的合成测量约0.41/1.78/3.41秒，包含路径检查和报告构建，不能当作生产SLO或纯关联耗时。

主代理选择在现有完整校验之后建立一次内存索引。数据库索引不能消除已读入列表的重复扫描，按版本重新查SQL会增加查询和改变当前快照装载路径，因此本轮不采用。选择的代价是增加O(J)级行引用/桶内存，仍需保留不可避免的报告输出及路径检查成本；这不意味着整个审计已变成线性时间。

## 精确行为

- `_collect_database_references`保留既有表检测、SQL和全部行校验；在校验jobs之后，仅把原来满足material过滤条件的行按`int(version_id)`存入`material_jobs_by_version: dict[int, list[dict]]`，作为snapshot内部新键。保留原`jobs`列表，桶保存原行引用及原id顺序。
- 过滤条件逐字保持：`job.get("job_scope", "material") == "material"`和`job.get("job_type", "material_index") == "material_index"`。缺列采用默认值；显式NULL、空串、其它大小写或其它文本不匹配。不存在的version对应的job不产生新报告，但仍须先校验，不能被过滤掉非法元数据。
- `_diagnose_version`只查`material_jobs_by_version.get(version_id, [])`，原active状态判断、missing/active/present_terminal、statuses顺序、pending job_ids及所有findings保持。
- job关联部分达到一次装载校验及一次索引，后续每个版本只遍历自己的桶；总成本O(J+V+匹配行)，增加O(J)内存引用。确定性测试计数全局jobs列表遍历/行访问和装载次数，不以壁钟阈值验收。
- `audit_staging(database: Path, versions_root: Path)->dict`、CLI参数/退出码、完整JSON值及列表顺序保持。无时间戳或新报告字段。只允许测试golden把临时绝对路径前缀规范成`<TMP>`，其余内容不忽略。
- 缺失jobs表、可选列缺失、空表、混合scope/type、活跃/终态、孤儿jobs以及非法id/version_id/status/scope/type必须保持旧版的结果或unsupported_schema错误。尤其不能因为非material或无版本而跳过非法行校验。

## 边界与验证

保留SQLite `mode=ro&immutable=1`、query_only、WAL/SHM拒绝、链接/owner/归档检查和inspect_only；不修改数据库schema、不写快照/版本目录、不加载真实.env、不导入应用管理器、不提供apply/rehome。不触碰运行实例、业务库、模型或Docker。

实施分为互不重叠的产品/复杂度测试、完整报告兼容golden、合成benchmark准备三路；主代理负责Git、统一冻结、独立复审、运行验收和交接。计数回归须先在旧实现取得RED；等价golden先验证旧实现，再验证新实现，不伪造其RED。

主代理冻结后运行全部审计/恢复集成/架构相关测试、全树Ruff、生产锁纯标准库CLI烟测及相同快照的旧新报告/耗时对照。此前3248通过/4跳过仅是基线，不记为本轮结果；本轮不重复与此纯审计模块无调用关系的整个后端。保留所有失败/成功日志、SDD和工作树，不自动合并、推送或部署。
