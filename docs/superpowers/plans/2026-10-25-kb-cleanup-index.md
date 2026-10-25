# KB Cleanup Snapshot and Directory Index Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Root coordinates parallel disjoint work; product tasks on the same files execute sequentially.

**Goal:** 复用目标版本快照并消除目录集合重复扫描，保留完整安全校验与错误优先级。
**Architecture:** Task1先完成调用内版本loader缓存，Task2再优化目录算法；Task3独立计划/错误兼容测试与Task4基准准备可和产品任务并行。root统一Git、复审、冻结与验收。
**Tech Stack:** 锁定Python3.12、stdlib SQLite/Path、pytest、Ruff。
**Spec:** `docs/superpowers/specs/2026-10-25-kb-cleanup-index-design.md`。

## Global Constraints

- 基线已双推main`1d413edbb1f6f7cc1d3bd4535e4ca85795680b58`；C隔离分支，D业务目录/进程/真实.env/DB/向量不改，不运行cleanup/rehome/模型/Docker。
- `resolve_kb_document_paths`仅新增可选`version_loader`；默认行为保持。缓存只在单次collect内、懒加载、空列表也缓存；原manifest优先级和全局外部引用读取保持。
- 目录逻辑使用Path父级集合，不用字符串前缀；absolute/resolve双候选及异常fallback、文件优先、链接/owner/类型检查不变。
- 所有原始证据写`artifacts/kb-cleanup-index-2026-10-25/`，SDD仅本计划目录；永久测试不依赖Git/artifacts。guard先导入、BelowNormal、BLAS1、新短系统basetemp、正常conftest，至多2worker，不覆盖原日志。
- Luna不做Git/派生子代理/timer/submit；root唯一阶段timer`T-A59BBA08AF82`。不清理旧工作树、环境或SDD。

## Review Focus

- 同一permission有多个KB目录行、无目录行或空版本列表：Task1真实SQL/loader计数每个ID至多一次；下次collect重新读取。
- 归档source错误与版本路径错误同时存在：Task1/3固定先报告原archive错误，不因预加载而改变。
- 同名兄弟目录、目录自身、根/大小写/点段和resolve异常：Task2 old/new关系谓词对照及边界测试。
- 目标外版本/manifest共享任一计划目录后代或目标身份：Task3完整错误对照，禁止借提速移除全局安全查询。
- 旧helper其它调用者和删除前置无副作用：Task1默认loader兼容，Task3/统一gate保持文件/DBhash、owner/链接/文件类型及合法缺失重试回归。

## Environment

开发PY=`artifacts/review-backlog-2026-10-24/env/developer-python/Scripts/python.exe`，生产PY同env下`runtime-python/Scripts/python.exe`。PYTHONPATH含repo、`artifacts/kb-lifecycle-concurrency-2026-10-24/tools`、`artifacts/staging-recovery-2026-10-25/verification/tools`；pytest先`-p offline_network_guard -p staging_test_priority`，统一xdist加已有test-tools。root已导出两份基线模块到`artifacts/kb-cleanup-index-2026-10-25/baseline/`及sources.json。

### Task 1: Operation-local lazy version snapshot

**Files:** Modify `rag/kb_document_cleanup.py`、`rag/kb_lifecycle_cleanup.py`；Create `tests/test_kb_cleanup_snapshot_queries.py`。
**Interfaces:** helper新增keyword-only `version_loader: Callable[[int], list[dict]] | None = None`；collect内部局部loader共用于resolver和补充permission循环，无新持久对象。

- [x] 用真实合成DB/collect入口写查询计数RED：重复目录行指向同permission、目录/permission双路径、无目录permission、空列表、多个版本；每ID最多查一次、collect再次调用会重新取新版本。不得stub预制计划。
- [x] 实现可选loader和懒缓存，不提前SQL、不改原全局读取/manifest/source/owner/路径/错误序；默认helper不注入loader时使用原方法。
- [x] 定向GREEN及helper默认兼容/归档错误优先级守卫，Ruff/新测试format/compile。报告task-1-report.md，root提交并fresh规格/质量复审后Task2才可改同一产品文件。

### Task 2: Parent-index directory membership and deduplication

**Files:** Modify `rag/kb_lifecycle_cleanup.py`；Create `tests/test_kb_cleanup_directory_index.py`。
**Interfaces:** 原`_path_references_cleanup_target`签名保持；新增`_minimal_cleanup_dirs(paths:list[Path])->list[Path]`，collect消费其返回值。

- [x] 旧版目录扫描/去重计数RED：D=1/16/64，负匹配和深层后代，实际算法输入/迭代次数不随D全扫描；测试不可仅镜像实现，需关系结果与原谓词对照。
- [x] 保持候选absolute/resolve及fallback，替换目录扫描为自身/parent集合查询；保留原sorted(set,key=len)及列表序，去重按严格parent集合判定。
- [x] 覆盖目录自身、根、兄弟前缀、重复/嵌套/相对、点段、resolve失败及平台大小写；在collect路径确认调用新去重helper。定向GREEN、Ruff/新测试format/compile，root提交与fresh复审。

### Task 3: Independent complete plan and rejection compatibility

**Files:** Create `tests/test_kb_cleanup_index_compatibility.py`、`tests/fixtures/kb_cleanup_index_expected.json`。
**Interfaces:** 只消费`collect_kb_cleanup`与原异常契约，合成SQLite duck-adapter须执行真实SELECT且正确ORDER BY；期望由冻结旧模块生成，最终测试只依赖repo JSON及Temp数据。

- [x] 创建完整计划golden：多permission/版本/重复目录行、material与document staging、dataset、归档source、外部不冲突行及合法缺失路径。比较所有返回字段/列表顺序，仅规范临时路径前缀。cleanup_dirs原同深度顺序依赖Path哈希：以golden冻结成员映回本次Temp后用旧算法生成该字段预期顺序，不重排实际输出，其它字段直接比golden。
- [x] 拒绝矩阵：错owner、外部版本或manifest共享文件/目录/身份、manifest身份错配、全局旧archive源与坏版本同时存在、symlink/junction或目录当文件；原拒绝代码与先后保持。平台链接无法创建须明确记录，不悄悄略过安全断言。
- [x] 冻结基线模块必须绑定冻结版helper函数以避免import当前优化helper混作旧oracle；验证sources.json哈希并记录。旧基线先GREEN，当前产品完整patch/compile后GREEN；这是语义守卫，不伪造RED。collect不执行删除，前后DB/文件树hash保持。
- [x] 正常conftest定向、Ruff/format/compile，报告task-3-report.md；root提交并fresh复审。

### Task 4: Synthetic benchmark preparation

**Files:** Create ignored `artifacts/kb-cleanup-index-2026-10-25/benchmark_cleanup.py`、本计划`task-4-report.md`；不改产品/永久测试。
**Interfaces:** old/new绑定同一显式合成SQLite/目录，冻结旧helper绑定保证oracle；guard和低优先级先于产品导入。

- [x] 准备N=20/100/300目标permission，各含目录行/版本，另有外部版本/manifest；原/新collect只生成计划，不调用删除。交替各3轮，完整结果或拒绝错误一致，输入hash保持。
- [x] 记录实际版本查询数、可观测目录关联访问数、耗时/峰值内存/结果大小及source/hash/priority；无壁钟通过阈值，不推断生产SLO。目录计数不能稳定观测时明确限制并用Task2确定性测试作主证据。
- [x] compile-only准备报告，root最终冻结和复审后执行并保存退出码/原始结果。

### Task 5: Root integration and handoff

**Files:** Update本计划、总顺序计划、docs/README.md、docs/handoff-2026-10-24.md；Create `docs/reviews/2026-10-25-kb-cleanup-index.md`。

- [x] 任务复审通过后整分支复查；实际发现集中一个最终修复波次和一次增量复审，冻结源/测试。
- [x] 正常conftest统一`tests/test_kb_cleanup_snapshot_queries.py tests/test_kb_cleanup_directory_index.py tests/test_kb_cleanup_index_compatibility.py tests/test_kb_cleanup_ownership.py tests/test_kb_lifecycle_integration.py tests/test_kb_lifecycle_api.py tests/test_document_versions.py tests/test_document_security_boundaries.py tests/test_architecture_budget.py`，至多2worker；Ruff与新测试format/compile，保存JUnit/exit/log。
- [x] 原生运行Task4旧新基准及生产锁cleanup-plan smoke，文件/DB输入不变、错误一致；只读原服务identity/ready/env hash核对。
- [ ] 完善所有证据/裁决/剩余条件，选择性提交、实际submit阶段唯一中文工作记录。保留可审阅分支、环境和证据，更新总计划Phase2后继续Phase3，不在此结束整个用户请求。
