# Material Locator Full Overhead Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. User explicitly requests parallel Luna; root owns design, Git, recorder and integration. Three scopes are disjoint; Task2 GREEN waits for Task1 READY.

**当前状态：** 实现/独立复审/整分支复审通过；冻结b50af49，统一84通过/0跳过、生产锁4项通过、Ruff通过。正式配对矩阵受1GiB可用内存门禁停止，保留partial，尚未完成性能验收；代码仍本地，未再次合并/部署。详见[结果与限制](../../reviews/2026-10-25-material-locator-overhead.md)。

**Goal:** 在完整权限/输出等价前提下减少材料定位全量载入与中间映射。
**Architecture:** Task1新增真实SQL授权投影/可见迭代器；Task2接入默认定位快路径、保留custom兼容；Task3独立完整等价与配对基准。旧full reader保留给organizer与冻结baseline。
**Tech Stack:** 当前Python3.12/SQLite/UserDB/pytest/Ruff，无新依赖。
**Spec:** `docs/superpowers/specs/2026-10-25-material-locator-overhead-design.md`。

## Global Constraints

- 基线main/两远端542a36f，C分支codex/material-locator-overhead-2026-10-25，D服务原PID48512；本轮不merge/push/deploy/restart、不读取真实.env/业务DB/资料，不改依赖、schema或organizer行为。
- root timerT-65C48915576F，子代理不Git写/timer/submit/派代理，只改自己的文件/本SDD。读其他计划SDD禁止。所有日志artifacts/material-locator-optimization-2026-10-25，旧环境/证据保留。
- org READ先于admin；legacy名称投影、KB MIN(id)分组顺序、同名/共享、live/current条件不变。eager完整model_input、完整semantic allowed IDs、FTS全局limit/顺序、exact→FTS→semantic去重不变，无跨请求cache。
- callback两参和构造后覆盖保持；custom版本snapshot/fallback-before-filter顺序保持，不用TypeError猜签名。默认路径才减少dict/version映射；不以惰性字符串/截断冒充提速。
- Python=`artifacts/review-backlog-2026-10-24/env/developer-python/Scripts/python.exe`，runtime同env/runtime-python。PYTHONPATH显式repo+artifacts/kb-lifecycle-concurrency-2026-10-24/tools+artifacts/staging-recovery-2026-10-25/verification/tools；pytest `-p offline_network_guard -p staging_test_priority`，BLAS1/BelowNormal、新短basetemp、不改TEMP。总最多2测试worker。
- 配对基准仅root在源码冻结和pytest结束后运行；单进程/最多2查询threads、free>=1GiB/WS<=256MiB软门禁、全自建fixture/网络guard，不能跑旧业务实例或调用模型。

## Review Focus

- SQL权限捷径改变legacy名称/同名/共享语义或把None ID当full：Task1真实SQL名称对照与缺ID只public场景。
- 过滤后KB重排/新出现KB被all-full分支放行：Task1 group order来自未过滤MIN(id)，all-full只yield已判定KB；Task3完整字符串/结果序对照。
- custom callback/row shape被默认路径吞掉或错误顺序变化：Task2构造注入/实例覆盖/模块patch、快字段与fallback、snapshot-before-filter回归。
- allowed IDs或model_input按top_k/FTS截断：Task2/3全部字段、原字符串、semantic集合/调用次数和FTS参数对照，不只比返回ID。
- 数据与计时被弱化：Task3同DB/完整结果/全表hash/真实计数，旧新交替；保留many-KB/admin及异常最大值，不将组件结果当SLO。

### Task 1: SQL visibility projection and per-request iterator

**Files:** Modify `rag/repositories/material_repo.py`；Create `rag/materials/locator_visibility.py`, `tests/test_material_visibility_projection.py`。
**Interfaces:** Spec两Repository方法签名逐字使用；导出`iter_visible_material_documents(db: UserDB, user: dict) -> Iterator[tuple[int, dict]]`。row字段/列序与旧list_material_documents_with_current_version相同。旧full reader保持语义，允许提共享SELECT常量。

- [x] 先真实RED：live/current KB顺序、legacy owner/public/shared(view/edit)/同名名称投影、无current/删除、case敏感名称、None用户不提权，org允许/拒绝与admin分支优先、all-full新KB不放行；新方法不存在/旧全量行加载触发有效失败。
- [x] 最小实现SQL名称子查询、显式apply_legacy_acl bool与完整行投影；iterator按当前KB权限计划取行，all-full批量回退只覆盖已判定KB。不要调用逐文档permission/current-version方法或缓存跨请求。
- [x] guarded串行GREEN本新文件+既有test_material_batched_access.py；真实Cursor/结果加载计数证明隐藏KB/行不做Python文档转换，SQL次数和all-full路径可审计。Ruff/format，自审报告本SDD task-1-report.md；接口READY通知root和Task2，不Git。

### Task 2: Default locator allocation reduction and custom compatibility

**Files:** Modify `rag/materials/locator.py`；Create `tests/test_material_locator_overhead.py`。
**Interfaces:** 消费Task1 iterator；_visible_documents保留完整dict行为可薄包装iterator，稳定原callback标识区分自定义函数，MaterialLocator构造参数/结果类不改。

- [x] 先写真实RED，默认路径全量dict/version结构负担的确定性访问/分配边界；constructor自定义、instance_visible_docs覆盖、module patch、custom两种row与fallback字段snapshot、fallback-before-filter/出错、subject0/self/None、多条件/path大小写/Unicode/NULL语义，完整model_input和semantic allowed IDs。
- [x] Task1接口READY前可写自己测试/代码，不启动依赖缺失的GREEN；默认单次筛选建docs、直接current字段构结果、只custom保留原versions快照，subject None省无效exact扫描。无semantic可免未消费集合；有semantic完整允许集合/调用次数不变，FTS全局参数与结果顺序不变。
- [x] guarded串行GREEN本新文件+tests/test_material_locator.py/test_material_batched_access.py，Ruff/format。报告task-2-report.md，明确实际RED/GREEN及default/custom边界，勿改Task1文件或公共测试夹具。

### Task 3: Independent full equivalence and paired benchmark preparation

**Files:** Create `tests/test_material_locator_equivalence.py`、`tests/fixtures/material_locator_expected.json`、`tests/fixtures/material_locator_542a36f.py`（root已提供冻结旧源，禁止修改，SHA见artifact baseline-source.json）；ignored artifact/benchmark工具/准备日志；报告本SDD task-3-report.md。
**Interfaces:** 基线为实际已交付542a36f的locator.py（root提供SHA锁定tracked fixture副本，CI测试不依赖ignored artifact或完整Git历史），旧reader保留可调用。导出完整结果/citation/rank/source/顺序、model_input原字符串、semantic集合与调用args的规范比较，不按排序抹平列表差异。Task1/2修改文件不能触碰。

- [x] 从真实旧source和合成fixture产生可复核golden/兼容测试，覆盖两用户legacy/org/admin、share/view/edit、重复名/跨KB顺序、过滤组合、删除/历史/无版本、custom两形状/回调覆盖、exact/FTS/semantic重叠与超top_k允许集合；旧新输出/权限拒绝及诊断完全对照。数据全合成，不弱化既有测试。
- [x] 准备资源受限配对harness：同fixture旧新交替N1000/5000/10000,C1/C2,3轮,每worker10测量+3预热；多KB/admin小case验证SQL开销权衡。每次完整结果/model_input相等，semantic完整集合另做确定性探针；全表逻辑hash/SQL与实际加载行计数、raw时间/最大值/样本数/资源/两源码SHA保留。不改老helper、不拿旧独立矩阵当新配对baseline。
- [x] 仅N20/单线程小自检及原冻结baseline测试（不跑正式矩阵）；Task1/2 READY后运行等价新测试。准备root确切命令与真正exit/guard自检证据，先证明无mock权限/无截断/无模型外呼。Ruff/compile；正式矩阵root冻结后一次运行。

### Task 4: Root review, isolated verification and closure

- [x] 分项规格/质量与原implementer修复/增量复审，最终一次整分支复查及必要单次修复波次，冻结源码。
- [x] 新3文件+既有locator/batched/organizer/FTS/org/doc-security相关统一回归、Ruff/format、生产锁实际locator API烟测；不在D业务配置下测试。
- [ ] **deferred_resource：前三次正式尝试均退出2，完整性能验收待补。** pytest结束后root成对基准，核对完整等价/全表hash/真实计数和资源；按实际值报告收益/代价，未达改进则回到证据定位而非删保护或改口径。
- [ ] **文档已更新，公司记录待实际submit。** 更新交接、计划、验收/裁决和公司实际submit；原服务PID/start/ready/env hash保持。源码留本地新分支可审阅，不自动再次合并或部署；main/两远端已交付542a36f事实保持。
