# KB清理目标版本快照与目录查找优化（2026-10-25）

状态：产品实现、三个分项独立复审及目录测试增量复审已完成；整分支复查和root统一验收待完成。代码仅在C隔离分支，D main/两端仍为已交付`1d413ed`，本轮不执行实际业务清理或运行部署。

## 范围与行为

[设计](../superpowers/specs/2026-10-25-kb-cleanup-index-design.md)、[实施计划](../superpowers/plans/2026-10-25-kb-cleanup-index.md)及[总顺序](../superpowers/plans/2026-10-25-sequential-followups.md)约束本轮。基线`1d413edbb1f6f7cc1d3bd4535e4ca85795680b58`，分支`codex/kb-cleanup-index-2026-10-25`。

`resolve_kb_document_paths`新增可选keyword-only version_loader，默认继续调用原UserDB方法；`collect_kb_cleanup`创建调用内惰性缓存，共用于KB目录解析和permission补充循环，空列表也缓存，下一次collect重新取数。没有提前查询，manifest identity/source仍先于版本路径校验；全局permissions/versions/manifests及所有外部引用检查保持。

目录引用保留空值、default_root、absolute/resolve双候选和resolve异常fallback、文件优先；目录匹配改为自身/parents在集合中。嵌套目录去重保留原sorted(set,key=len(parts))的顺序，以严格parent集合过滤。原cleanup执行、SQL teardown、gate/授权/tombstone、链接/类型/owner校验未改。

## 分项证据与复查

- Task1提交`f241fe6`：真实UserDB重复目录路径的旧版本查询3次/permission，经RED后改为单次；新3项测试涵盖有版本和空列表的重复目录行、无目录permission及跨collect刷新，正常conftest3通过。默认单文档helper及archive错误优先级另有2项原回归通过。规格/质量复查PASS，无发现。
- Task2提交`63cc734`：原目录关联在D=1/16/64实际遍历1/16/64个计划目录，计数RED后改为祖先集合；新目录测试和Task1/3组合21通过。规格PASS，提出去重缺少规模计数的P2测试缺口，测试修复`19a7b29`后D=1/16/64旧关系调用0/240/4032，新parent访问8/128/512（该次depth=8）、新is_relative_to调用0，完整顺序同旧oracle。目录文件10通过，增量复审ADDRESSED、无新问题，产品代码未再修改。
- Task3提交`1295f9e`：冻结基线两模块hash锁定，lifecycle五个helper显式绑定冻结版本，避免混入新helper。1份完整计划和10类拒绝golden，旧/新各11通过；只规范Temp前缀，cleanup_dirs同深度顺序用冻结成员及旧算法生成本次Path哈希顺序，未排序实际输出或忽略字段。前后DB/目录hash保持，独立复查PASS。一次无输出运行中断未能复现，完整当前11项及faulthandler单例均通过，原日志保留，不归因为产品或适配器缺陷。
- 目标产品文件的旧全文件format差异保留；Ruff lint、新测试format和compile通过，未为本轮扩大格式化范围。

## 验收准备与边界

统一冻结后的9文件正常conftest回归、Ruff、生产锁4项cleanup-plan smoke、N20/100/300旧新基准及服务只读核对待补。本轮不把已发布main的3263/4基线算作新结果。

基准使用真实SQLite SELECT和实际版本查询计数；早期准备脚本曾以内存列表代替目标版本查询，root检查后在任何执行前改正。随后发现既有store→spreadsheet parser依赖真实config模块；使用无.env的C源码，并在导入前安装审计hook拒绝任何.env文件open，禁止UserDB/materials导入，验证实际config.__file__在C源码内。配置对象仅满足既有解析器导入，不输出设置值，不使用其默认存储路径；全部DB/根目录仍为显式合成输入。脚本未运行前仅报告compile，不伪造性能结果。

原始证据位于Git忽略的`artifacts/kb-cleanup-index-2026-10-25/`，冻结基线及所有成功/失败日志、原SDD和环境保留。各分项定向数字存在重叠，不相加为统一总数。

## 主代理裁决及代价

1. 单操作惰性loader缓存而非预加载/持久缓存；代价是新增可选内部参数及目标行引用，沿用既有KB gate排斥目标写者的约束。
2. Path自身/祖先集合查询并保留原去重顺序；代价是路径深度工作和O(D)集合内存，文件系统安全扫描及外部引用读取仍有成本。
3. 保留原同深度Path/set哈希顺序；代价是兼容测试包含一个小的旧算法预期计算，不承诺新的跨进程稳定顺序。
4. 两个产品任务顺序修改同一文件，兼容测试/基准准备独立并行Luna；代价是目录任务等待缓存复查，root协调冻结和Git。
5. 第二阶段源码保留隔离分支；代价是后续集成仍需明确的交付动作，未扩大第一阶段main发布为自动部署。
6. 保留SDD/环境/证据，使用原生辅助脚本；代价是本地磁盘及root的包/目录归属核对成本。
7. 允许既有解析器必需的真实C config导入，以文件审计hook禁止dotenv IO并继续阻止UserDB/materials；代价是加载一个未被清理算法使用的Settings对象，验证范围不包含真实运行配置。

## 交付与接续

本轮实现尚未合并main、双推或部署。root唯一timer`T-A59BBA08AF82`待统一验收后实际submit记录。第二阶段结束后按总计划继续第三阶段403/Compose契约、历史恢复写流程，再推进容量/SLO和长期数据架构，不能在第二阶段结束时把整个用户请求标为完成。
