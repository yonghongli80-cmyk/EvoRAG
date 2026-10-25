# KB清理目标版本快照与目录查找优化（2026-10-25）

状态：产品实现、三个分项独立复审、目录测试增量复审、整分支复查及root统一验收通过。受测冻结`7c22af9f2f3bec05bad1f9ea7fbdd3b108b07ed7`，产品代码最后修改`63cc734`，之后测试修复及文档不改变产品。代码仅在C隔离分支，D main/两端仍为已交付`1d413ed`，未执行实际业务清理或运行部署。

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

冻结`7c22af9`后，九文件正常conftest、2 worker/loadfile、BelowNormal、BLAS1及offline guard统一回归：**86通过、1跳过**，66.32秒、退出0；JUnit87项、0失败、0错误。跳过仅为Windows没有fork方法。全树Ruff、三份新测试format/compile通过。所有产品/测试/锁文件相对冻结源无漂移；不把已发布main的3263/4基线算作本轮新结果。

生产锁解释器实际4项烟测通过：完整计划等价及版本查询4→2、默认单文档loader、外部后代引用拒绝、archive拒绝先于目标版本懒加载（查询0）。烟测只collect，不调用cleanup。首轮在collect之前因验证脚本错误绑定了当前`resolve_document_staging_directory`而被冻结来源检查拒绝；将该绑定改为冻结doc模块后第二轮退出0。失败日志及元数据保留，这只改了artifact驱动，不改产品，也不重复无关回归。

16:49:46（Asia/Shanghai）只读核对原PID48512及`2026-10-23T14:09:17.6870660+08:00`启动时间保持、ready200、SQLite/Qdrant local OK、可选Redis degraded；D.env SHA256保持`6128C4BD1F1BC0E43AB5189BAD25FA160CB8567B555B1B0CC5921E8771B0E1BD`。D main仍`1d413ed`且跟踪文件干净，C无.env，未重启/部署或访问业务存储。

基准使用真实SQLite SELECT和实际版本查询计数；早期准备脚本曾以内存列表代替目标版本查询，root在任何执行前改正。既有store→spreadsheet parser依赖真实config模块，验证驱动使用无.env的C源码，导入前安装审计hook拒绝任何.env文件open，禁止UserDB/materials导入，并验证实际config.__file__在C源码内。配置对象仅满足既有解析器导入，不输出设置值、不使用默认存储路径；全部DB/根目录为显式合成输入。基准脚本只在最终冻结、复查及统一gate之后执行，原始18次调用退出0，完整计划逐值一致，每组前后DB/树hash保持。

## 合成计划收集对照

每规模同一快照、旧新交替各3次，计时包含tracemalloc、真实SQLite查询及文件元数据检查，未并发运行pytest。目标各有一个KB目录行/版本，含外部版本及manifest；版本字节是合法缺失重试目标，不执行实际删除。

| 目标permission | 版本SELECT旧/新 | 全部SELECT旧/新 | 中位耗时旧/新(s) | 耗时减少 | 峰值中位数旧/新(bytes) |
| --- | --- | --- | --- | --- | --- |
| 20 | 40 / 20 | 67 / 47 | 0.330 / 0.221 | 33.0% | 88816 / 115773 |
| 100 | 200 / 100 | 307 / 207 | 3.161 / 0.989 | 68.7% | 427851 / 566899 |
| 300 | 600 / 300 | 907 / 607 | 19.181 / 3.581 | 81.3% | 1240287 / 1680439 |

这些是本机低优先级合成计划收集诊断，每种实现只有3次样本；不包含manager、Qdrant、文件删除及SQL teardown，不能当作整个KB删除提速或生产SLO。缓存和集合增加内存引用；实际目录树扫描/权限/外部引用校验成本仍在。集成基准没有稳定的目录关系计数hook，因此使用Task2确定性测试作算法证据，不伪造该计数。

`verification/verification-summary.json`关联实际退出码、JUnit、版本查询及原始结果。JUnit SHA256为`86f84744a46beba8cb03fb23251235d67577bf919865e60d5f364d765b62b538`，基准结果SHA256为`c4b0710a4634f53914ad43af6cbce5ba5099f2ed179c5d164dae1e184b7cf1ae`。成功运行使用支持脚本SHA256`527F053A54EEC74068F7680A624C1A4317E8BD320C22985C0393BE7A3B99E828`；static准备记录中的旧artifact hash及失败attempt1保留其当时事实，不覆盖为成功。

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

本轮实现尚未合并main、双推或部署。验收文档提交`acaa9f7d0e891e5a0e32aa84999be74c75893829`后，root已用唯一timer`T-A59BBA08AF82`实际submit，返回`R-20261025-884467AF`、`pending_review`；本地记录已创建，等待人工复核。之后仅补回执，受测源码保持。

第二阶段已完成，按总计划继续第三阶段403/Compose契约、历史恢复写流程，再推进容量/SLO和长期数据架构；不把第二阶段结束当作整个用户请求完成。
