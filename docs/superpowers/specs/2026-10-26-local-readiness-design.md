# 本地工程准备与启动治理设计

## 范围和当前授权

用户要求先做本地能完成的工作，服务器上线近期暂缓；此前明确修复后正式压测留待服务器、结果留空。本轮基于已双推 main `99834ca`，使用现有 C 隔离工作树的新分支 `codex/local-readiness-2026-10-26`。D 运行服务、真实配置/资料/数据库不参与；不重启、部署、业务迁移或启动正式压力负载。

本地交付分为四个独立子项目：可移植验收工具、API 启动职责与后期清理、静态 SQL/事务迁移清单、文档唯一当前状态入口。它们不启用 PostgreSQL、多实例、对象存储或生产 shadow；这些实现继续按已有 ADR 前置条件推进。服务器资源门禁实测、业务数据、供应商真实返回头及正式 SLO 仍未验收。

## A. 可移植材料定位验收工具

### 文件和 CLI

新增 tracked 标准库资源层 `scripts/material_locator_resources.py`、隔离/冻结/证据支持层 `scripts/material_locator_acceptance_support.py`、入口 `scripts/paired_material_locator_acceptance.py`，各文件保持职责明确且小于 1000 行；不得依赖 ignored artifact 模块或新增第三方包。历史 helper/样本/source fixture/golden 均只读保留。

CLI 使用 `python -m scripts.paired_material_locator_acceptance`，有三个明确子命令：`freeze --manifest <新文件>`、`check --manifest <清单> --output <新文件>`、`run --manifest <清单> --output <新文件>`。无默认负载命令；`check` 不导入 config/RAG、不创建 DB、不运行 locator。`run` 才会构造合成 fixture 和测量，本轮禁止调用。输出仅允许专属 artifacts 中的新文件/新 run 目录，拒绝覆盖、源目录或路径逃逸。

原 baseline fixture 固定 LF SHA256 `54593297c81a31000dee9c26336db3198bc85e3e334f001cddb7f33766434bfc`。Freeze 绑定 Git commit、完整 rag/tests/scripts/config/锁文件清单及工具，记录 raw 与 LF 文本哈希；核验这些目录 tracked/untracked 状态。跨 OS 比较已声明 UTF-8 文本的 LF 哈希，其它字节严格匹配；不把仅 locator.py 相同当作整个修复版相同。需要源 Git checkout，缺 Git/清单/文件/哈希即拒绝，不猜测容器内无 Git 场景。

为保留 preflight 失败证据，允许先在非源码 artifacts 中分配新输出并写 starting，然后核验 freeze；artifacts 不属于冻结源码根集合。其后安装离线 guard、优先级/线程限制和资源门禁，再建立干净合成上下文、禁用 dotenv/secret sources、SQLite 路径约束，最后才导入 RAG。基准 CLI 是独立子进程，可原地将自身环境收缩为 OS/Python 必需 allowlist，HOME/USERPROFILE/XDG 等指向本 run，所有 RAG 存储路径显式指向新 context；不打印或保留继承凭据。socket connect/connect_ex/create_connection 及 DNS 外联都拒绝，包括 8080，不允许以 loopback 为由连接业务实例。

### 资源与证据

资源函数 `sample_resources(*, platform_name=None, proc_root=Path('/proc'), sys_root=Path('/sys')) -> dict` 仅依赖标准库且可注入文本根用于单测；`enforce_resource_gate(sample, *, min_free_bytes, max_working_set_bytes) -> dict` 遇不可测、越界或优先级/BLAS约束不满足即拒绝。默认兼容策略为 1GiB/256MiB，但服务器运行前必须显式记录所选门槛，不能把它称为 SLO。

Windows 延迟加载 ctypes API，采可用内存/自身 working set并设置 BelowNormal；不声称覆盖 Windows Job 外层限额。Linux 从 proc 读取 host MemAvailable/RSS，结合 self/cgroup 与 mountinfo 映射 v1/v2 memory controller；读取当前节点及可见祖先限制，effective available 为 host 与所有有限 headroom 的最小值。严格处理 max/v1哨兵、挂载根、转义路径和路径边界；缺失/损坏/不匹配拒绝，禁止退回只看宿主。namespace 隐藏祖先必须报告可见性边界，不声称 OS 硬限额。Linux nice 至少 5并回读，不提高已有更低优先级。仅门禁必需字段不可测才阻断，其余环境信息标明不可用而不虚构。

基础矩阵保留 N1000/5000/10000×C1/2×三轮、每 worker 先3预热后每轮10测量：双 KB 主场景以及实际 N100 的 many-KB/admin，共18条 case、810对 timed 样本。**case路径必须包含 requested_N、实际N、case及C**，避免历史 helper 的 N100小场景跨 requested_N 目录冲突。额外 `--acl-extension` 单列一个 N100 混合 legacy/org/public/shared/hidden case，各 C1/2，共2条/90对；与基础样本分栏，不重解释旧数据。

保留完整有序字段/model_input逐对相等；另以确定性本地 semantic probe 比较完整允许版本集合和参数，不外呼模型。诊断、semantic、warmup、timed 计数分开；普通 probe 每实现每case一次（不是每worker），warmup/timed 才乘 C。AB/BA 整批交替，最大C2，不混跑旧新实现。全表逻辑 hash/行数前后相同；四个 repository 方法（含 access_plan）记录 calls/返回文档或 KB-ID 行，实际 SQL trace 和批量查询次数分开，不能叫全部 SQLite 行读取量。完成 round 必须先落盘再资源门禁；partial、错误、原样本及最大值保留，非零退出，不跨 run 拼接。

本轮只做纯解析/拒绝/调度/报告单测，以及无 RAG 导入的 `freeze/check` 本地预检；**不运行 run，不把 fixture/mocked Linux 分支当真实 Linux 验收**。实际服务器资源和完整配对门禁继续留空。

语义依据：[Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)、[mountinfo](https://man7.org/linux/man-pages/man5/proc_pid_mountinfo.5.html)、[v1 memory](https://docs.kernel.org/admin-guide/cgroup-v1/memory.html)；v1 文档自身有陈旧提示，采用保守可见性与拒绝规则，目标机仍须实测。

## B. API 启动职责和后期生命周期清理

把七个启动/迁移实现移至无 import-time I/O 的 `rag/api_startup.py`。API 保留同名同签名 facade：`_run_document_version_migration`、`_run_material_metadata_migration`、`_run_spreadsheet_migration`、`_register_repo_document_version`、`_run_legacy_kb_migration`、`_run_staged_version_migration`、`auto_index_on_startup`。每次传入当前 API module，helper 经该上下文动态访问依赖和其它 facade；不得反向 import rag.api、快照依赖或搬动 `_ApiModule` 兼容机制。新模块顶层只导入无 I/O 标准库/类型；原 folder/vector/migration 局部导入保持延迟，避免增大 eager import 图。api/pipeline/局部import既有预算不放宽。

单独复现/提交后期启动清理缺口：现 lifespan 的 try 在 backup/evolution start 与 rollout restore 后，restore 抛错会跳过清理。**只将 try/finally 提前至同步线程启动/迁移完成之后、三个后期启动 hook 之前**；不改变前期 to_thread 或正在执行的索引线程取消语义，避免线程未结束时提前关存储。启动成功顺序不变；后期启动失败、body错误/取消、正常退出均依次尝试 backup stop、evolution stop、close_pipeline_processors，保留既有 `_stop_material_worker()==False` 时不关共享资源的保护。任一清理错误不阻止后续清理；有原始错误/取消时保留原异常，无原错误时传播首个清理错误，日志不变成API成功。停止方法当前幂等；不启用任何真实 worker/模型。

先真实 RED：恢复 hook 失败时应清理的断言、清理失败不掩盖原错误/不跳后续的断言；再最小修复。纯拆分的新增边界测试证明 api_startup 可以在禁止 DB/网络/目录创建的子进程中导入，且 API动态patch和现有启动时序全保留；不以单纯源码字符串或行数作为行为测试。前期线程取消/排空协议不在本次行为变更范围，明确保留现有边界。

## C. SQL/事务可移植清单

新增离线 `scripts/audit_sql_portability.py` 和单测。默认只读取显式 rag 根的 .py/.sql，不读取业务DB、.env、artifact或导入应用；无 eval、exec、SQL执行、驱动安装或自动重写。Python用AST提取 execute/executemany/executescript 的字面量及纯静态字符串拼接，动态/f-string/变量/不支持表达式标记 unresolved，语法/读文件失败显式报告。SQL文件按方言标记扫描；注释/字符串处理保守并说明局限。

JSON按path/line稳定排序，记录 owner function、sink、静态/动态分类、SQLite特性（PRAGMA、AUTOINCREMENT、INSERT OR/REPLACE、rowid、FTS/MATCH/virtual table、strftime等）及事务/commit/rollback/read/write_connection位置。它是候选 SQL 静态盘点，不宣称全部SQL已解析或PG不兼容已证明。输出必须显式有 unresolved/coverage 字段，路径/行精确可复核，已有输出拒绝覆盖；诊断无改写/执行副作用。

配套 `docs/architecture/sql-portability-inventory.md` 和 `docs/architecture/unit-of-work-contract.md` 说明实际清单、当前分散事务与目标UoW、跨repo原子性、savepoint/整命令重试/outbox、合成golden与未来PG环境准入。只交付工具与迁移准备契约，不新增空壳主库adapter、不改变SQLite业务语义，不把文档或测试准备称PG支持。

## D. 唯一状态入口和延后事项

docs/README作为当前索引，置顶链接 `docs/local-readiness.md`，更正日期和“仍本地”等已被99834ca交付覆盖的索引文案。handoff顶加当前摘要；旧历史段不篡改数字/时间，只加历史标记。UPGRADE旧Docker不可用与COPY风险标明历史并指向已完成验收及当前构建排除约束。服务器清单在A实现后改为“工具代码/本地单测已准备、目标平台及正式负载未验收”，不勾选未运行门禁。

路线图清楚列出本轮完成范围及下一阶段：隔离PG原型/adapter、真实中文FTS与数据等价、共享epoch/fencing/对象存储/多实例、生产自动shadow。后者涉及业务内容、保留与成本约束，目前不将真实流量接入采样；可本地完成合成设计不代表已有运行功能。真实脱敏fixture与真实供应商证据不能本地编造。
