# 本地验收工具、API启动治理与SQL迁移准备验收

**本批本地实现与验收完成，尚未合并/推送/部署。** 分支 `codex/local-readiness-2026-10-26` 基于已交付 main `99834ca`，受测源码冻结 `d837501a741d8ddb431c29174e5a128c591e4097`；之后仅补结果与工作记录回执。服务器上线、业务迁移及正式压力/SLO验收继续按用户要求暂缓，性能数值留空。本报告不代表整个项目所有架构或业务验收已经完成。

## 本地成果

1. **Tracked 跨平台验收工具。** 三个标准库脚本分开资源采样、隔离/冻结支持和 CLI，明确 `freeze/check/run`。支持 Windows 资源 API 和 Linux proc/cgroup v1/v2 可见祖先限额分支；不可测即拒绝，不伪称 OS 硬限额。运行前清理继承配置、设置合成上下文，阻断网络/DNS和非本run SQLite；不依赖旧 ignored helper。非法run参数在输出、冻结或RAG导入前拒绝。
2. **证据与失败保留。** 基础18条case/810对及可选ACL扩展2条/90对分栏；小场景路径同时含requested_N、实际N、case和C。保留完整有序结果/model_input及独立semantic完整ID探针、全表hash、四方法分阶段计数。已完成round先落盘再gate；worker中途出错保留已完成的未配对观测并停止新请求，不把partial凑成完整paired结果。
3. **API启动职责拆分与后期清理。** 七个API facade保留实时module上下文、公共签名、兼容导出和原延迟依赖，迁移/启动实现移至 `rag/api_startup.py`；既有架构预算未放宽。修复后期启动hook失败跳过清理、首个cleanup错误跳过余项或覆盖原始错误的问题。正常顺序不变，原错误/取消优先，无原错误才传播首个cleanup错误；worker未停不关闭共享资源的保护保持。
4. **SQL/UoW本地准备。** 新只读AST/SQL工具不导入app、不eval/exec、不连数据库或重写SQL。输出静态条目、动态未解析项、SQLite特性、事务/代码标记和coverage；Python SQL位置使用真实参数表达式锚点，SQL相对行另列。UoW文档是目标合同，不是已启用的PG adapter。
5. **当前状态入口。** 更新文档日期、已交付与历史“仍本地”状态、旧Docker失败及已完成隔离验收的覆盖关系。当前入口为[本地状态](../local-readiness.md)，旧报告数字/时间不篡改，服务器清单没有勾掉未执行项。

## 权威验证

| 检查 | 实际结果与范围 |
|---|---|
| 完整后端离线回归 | **3423 passed / 5 skipped，1966.16秒，实际退出0**；JUnit3428项，0失败、0错误；单进程、BelowNormal、BLAS1、新OS短Temp |
| 跳过项 | Windows缺fork、缺FIFO及terminate不能忽略SIGTERM三项；另两项真实脱敏表格fixture缺失。未删断言或用合成数据冒充真实fixture |
| 生产依赖锁启动烟测 | **5项通过、0失败、实际退出0**：纯startup模块导入、后期失败清理、primary错误保留、正常退出首cleanup错误传播、worker未停时保留资源；真实API门面、合成hook/临时DB，无HTTP/provider/负载 |
| 分项/修复及整分支复审 | 三个代码范围独立审查及增量修复复审通过；一次最终整分支审查和唯一合同修正增量复审通过，无未关闭已确认发现/minor |
| 静态检查 | 全树Ruff实际退出0；未改依赖锁、前端、schema或业务配置 |
| 源冻结 | `freeze/check`前后均退出0，605个冻结文件回归后仍匹配，manifest SHA256 `e49ea679589ad633999d28693bfe3b2f0764c05e5f8efc522ad8cc0333182f14` |
| Windows资源调用 | 实际采样API与自身BelowNormal设置退出0；仅证明采样可读，不表示当前资源门槛放行，更不是Linux/服务器或负载验收 |

分项用例有真实RED/GREEN：工具最终27项fixture单测、API相关37项及另1项facade检查、SQL10项fixture单测。未把这些数字相加冒充完整后端；上表完整结果来自同一冻结版本的一次完整结束运行。生产锁烟测使用7751f66之后未再改变的API/支持层，最终源码已核对消费文件等价。烟测stderr中的两次cleanup traceback来自预期注入的合成错误，实际退出0并验证原错误优先。

失败与修正原件均保留，包括初始cgroup fixture把“当前节点无限制”误当“全层级无限制”、imports/兼容导出lint、SQL多语句事务漏标和位置映射、worker失败丢样本及非法参数校验顺序。首个根代理提交命令的PowerShell插值语法错误没有执行Git或修改产品，修正后才提交；不将工具或fixture错误写成生产缺陷。

## 真实静态清单

源码扫描208文件（204 Python/4 SQL），得到850条静态SQL、349个事务位置及50个代码可移植性标记；103项unresolved由36个dynamic expression、65个f-string及2个unsupported expression组成，0读取/语法错误。CLI实际返回1，明确表示覆盖不完整，**103项不等于103个已证实bug，也不能据此判定PG兼容**。

清单绑定283887e的rag/scanner内容；最终候选已核对这些内容未变。原始JSON在 `artifacts/local-prep-2026-10-26/verification/sql-inventory-round1.json`，接口和局限见[清单说明](../architecture/sql-portability-inventory.md)及[UoW合同](../architecture/unit-of-work-contract.md)。未来代码变化需另建清单，不覆盖本次记录。

## 裁决与代价

| 裁决 | 原因与代价 |
|---|---|
| 本批完成四个可闭环本地子项目，不顺带改全库PG或接生产shadow | 避免未经等价和数据/预算约束的大迁移；PG、多实例与生产采样的实际实现仍留在后续路线图 |
| CLI显式freeze/check/run，先在独立artifact写失败起始状态再核对精确源码根 | 无负载预检与正式run分开，失败也留证；output不在冻结源码根，不能拿本次check代替服务器验收 |
| Linux只对可见cgroup祖先取最小headroom，不可测拒绝 | 不把宿主空闲当容器空闲；namespace隐藏边界和真实目标机能力仍需实测 |
| 核心18/810和ACL2/90分开，旧helper/数据不改 | 保持可比口径并避免小场景目录冲突；新工具要在服务器创建全新freeze/run，不拼旧partial |
| 结构化worker partial后统一落盘，不每call重写整个大JSON | 保存已完成/未配对结果且不扭曲paired计数；不承诺OS突杀时逐call持久性 |
| API仅修两次to_thread结束后的清理边界 | 防止线程还在使用存储时提前关闭；前期线程取消/排空协议保持原边界，需独立治理 |
| SQL使用实际表达式锚点和SQL相对位置，不做完整token反向映射 | 不编造物理源码行；定位精度有限，人工核对时需同时看表达式和相对位置 |
| 最终轻微参数顺序问题属于原spec合同，补一轮有RED的最小修正 | 未升级问题严重度或新增业务范围；付出一次定向验证以避免非法参数触发无谓应用初始化，文档状态同步更新 |

## 保留与未完成边界

原服务PID48512/start、ready200、D .env哈希及D main99834ca/跟踪树在回归后核对保持。未读取业务资料/数据库、重启/部署、运行正式 `run` 或执行业务恢复迁移。新分支仍本地可审阅，原环境、历史helper/样本、所有SDD和原始日志保留；本轮非DB证据另做逐文件hash归档。

正式Linux/cgroup/容器实测、服务器完整矩阵与P50/P95/P99/吞吐/容量、业务SLO、两项真实fixture、实际供应商返回头、PG主库/共享一致性/对象存储/多实例和生产shadow均未因此完成。下一阶段次序见[本地状态与剩余路线图](../local-readiness.md)，服务器压力门禁见[验收清单](../material-locator-server-acceptance.md)。

公司记录在实际提交成功后补回执。初始工具勘查曾由子代理单独提交咨询记录 `R-20261026-C0FFC07B`（无timer，含一次无JSON失败后实际成功），后续明确仅root记录；本轮实现/审查/整合记录与该静态咨询分开范围，不重复登记咨询为实现成果。
