# 材料定位服务器性能验收清单

**状态：服务器压测未执行，未通过性能验收。** 本地隔离分支已准备tracked Windows/Linux工具、fixture单测与分项复审，正式完整后端验证和本机只读freeze/check结果见[当前本地状态](local-readiness.md)。真实Linux/cgroup与服务器负载仍未验，没有修复后的性能收益、容量或SLO结论。

本轮仅代码正确性受测冻结为 `7200e3040e77355c5859dfb5ea03383275301d5e`，见[整改报告](reviews/2026-10-26-material-locator-many-kb.md)。服务器执行时仍须绑定实际部署版本。旧 source fixture 的固定 **LF 标准化** SHA256 为 `54593297c81a31000dee9c26336db3198bc85e3e334f001cddb7f33766434bfc`；各文件原始字节哈希另记，跨 OS 不混淆 CRLF/LF 口径。

## 开始前：绑定被测对象与环境

- [ ] 记录新旧源码冻结信息：仓库、基线 commit、修复版 commit，以及产品和测试 Git 树的完整冻结状态。记录并核验 tracked diff 与 untracked files 均为空（或将其明确纳入冻结快照），并保存完整树的可复核标识。至少单独记录 `rag/repositories/material_repo.py`、`rag/materials/locator_visibility.py`、`rag/materials/locator.py`、本次相关测试文件、旧 source fixture 和 benchmark/helper 的 SHA-256；不能只用 locator.py 的 SHA 代表修复版。修复版最终 commit 由 root 在冻结时填写；如尚未提交，则启动前绑定实际冻结快照和文件哈希。源码、测试或 helper 变化后须重新冻结，不能沿用旧结果。
- [ ] 绑定依赖环境：OS 发行版及版本、内核、CPU 型号/核数、物理内存、Python 精确版本、SQLite 版本、锁文件及依赖实际版本、关键线程环境变量、运行用户与容器/虚机资源限制。保存可复现的依赖清单和运行参数。
- [ ] 固定 fixture 生成器版本、参数、随机种子（若适用）及生成数据摘要。所有旧新比较使用同一份真实 SQLite fixture 的副本或只读快照，确认 schema、行数、索引和逻辑内容一致；不连业务数据库，不使用真实资料或生产 `.env`。
- [ ] 在目标服务器确定资源停止门槛、运行时段及并发限制，并记录依据和执行负责人。不得把旧 Windows 工作站的资源门槛直接当作服务器门槛，也不得临场降低门槛以完成矩阵。
- [ ] 确认负载进程与业务服务、真实环境变量、数据库、资料目录和向量服务隔离。无外部模型/API 请求；若运行需要应用初始化，显式指向本次新建的合成数据目录并验证路径。
- [ ] 确认目标平台实机门禁通过：历史 `paired_benchmark_task3.py` 仍为Windows专属并保留不可变；新tracked入口 `scripts/paired_material_locator_acceptance.py` 分开资源/支持层，包含Linux v1/v2及可见祖先限制的分支和fixture测试。源码准备不等于实机通过，需在目标机核验真实proc/cgroup/priority与namespace可见性边界，再绑定最终helper哈希；本轮未执行正式负载。

## 本地只读准备命令

每次换一个新的run目录，以下两条不运行locator或创建DB：

```bash
python -m scripts.paired_material_locator_acceptance freeze --manifest artifacts/material-locator-acceptance/new-server-run/freeze.json
python -m scripts.paired_material_locator_acceptance check --manifest artifacts/material-locator-acceptance/new-server-run/freeze.json --output artifacts/material-locator-acceptance/new-server-run/check.json
```

正式 `run` 另需明确 `--min-free-bytes`、`--max-working-set-bytes`、`--counts 1000,5000,10000` 和 `--workers 1,2`，固定三轮/三预热/十测量；按需加 `--acl-extension`，独立增加N100混合权限case各C1/2（2条/90对）。门槛由目标服务器条件确定并记录，不把旧工作站默认值当生产SLO。本轮不执行该命令。旧fixture及历史partial不覆盖或续拼。

## 本地非计时功能验证

新增 `tests/test_material_locator_acceptance_functional.py` 用独立子进程和新建 Temp SQLite 检查四种合成 profile：双 KB 普通用户、many-KB 普通用户、mixed ACL、双 KB admin；每份 fixture 最多 8 篇。它对比冻结旧 locator 与当前 locator 的完整有序结果字段和 `model_input`，以 `top_k=1` 核对完整 allowed version ID 集合，并检查只读前后全表逻辑 hash、四种 repository 方法计数和按实现/functional phase 保存的 SQL trace。首个 diagnostic 注入异常用例会禁止 worker pool、计时器及资源采样路径，并检查 partial 状态和 DB 关闭。

合成 fixture profile 已升至版本 2。`construction_attributes` 中的 owner、owner-hidden、scope 与组织 KB 数量是构造维度；`effective_acl` 独立记录真实 locator visibility iterator 得出的可见/隐藏文档 ID 和数量。构造属性可能重叠，不得相加为 ACL 总数。mixed ACL 的 8 篇小 profile 包含组织拒绝 KB 中一篇 public 与一篇 shared 文档，用于确认组织拒绝优先于文档 scope。

上述用例是本地 SQLite 功能正确性验证，不包含预热、计时、吞吐/延迟输出或资源采样，不改变正式 18 case/810 对矩阵及 ACL 扩展 2 case/90 对合同。测试实现已准备；GREEN 运行状态以单独 Task2 报告记录。它不能替代真实 Linux 门禁、服务器压测或业务 SLO 验收。

## 配对矩阵

- [ ] 旧 case-loop 基础矩阵应为 18 条 case 记录：每个 requested_N/C 组合各含双 KB 主场景、many-KB(N=100) 和 admin(N=100) 小场景；总计 810 对 timed samples（1,620 个耗时值）。many-KB/admin 的 requested_N 仅为 case 标签，实际数据规模为 N=100，不计入大规模。新增 hidden 分布属于扩展矩阵，单独记录参数和样本，不并入这 18 条/810 对基础矩阵。

- [ ] 每个 case 在相同 fixture、相同参数下比较冻结旧版和冻结修复版，采用整轮 AB/BA 交替顺序，避免旧新并发互相争用。先确认每个 worker 的独立 locator/连接行为符合被测并发定义。
- [ ] 主矩阵：文档数 N=1,000、5,000、10,000；并发 C=1、2；每个组合 3 轮。旧版和修复版分别在每个 case 的每个 worker 先执行 3 次预热，随后各自每轮执行 10 次计时样本（每个实现、每 worker 共 30 个 timed samples）；另记录每个实现每 case/worker 的 1 次 diagnostic probe。C=1 时每个实现预期每 case/worker 共 34 次调用（1 probe + 3 warmup + 30 timed）。记录完整实际调用和样本数；中断、失败或未完成的 case 明确保留为 partial/failed，不能拼接成完整验收。
- [ ] 覆盖 KB 分布：主矩阵的双 KB 用户场景；另跑 many-KB 用户及 admin 场景。many-KB 应使用交错 KB 顺序，验证实际 KB 分组/批量行为。admin 至少覆盖与基线相同的规模上限小场景；若扩大规模，单独记录并说明，不能和原规模混算。
- [ ] fixture 明确包含并记录可见/隐藏资料的分布，包括无权 KB、无权文档、owner/public/shared 及组织允许/拒绝场景；记录每类 KB 和文档数量。确保新旧读取同一授权状态，并核实隐藏资料未进入返回结果或 Python 文档行。
- [ ] 对每次 probe、预热、计时调用保留完整输出等价证据：返回顺序、所有返回字段（含 citation/rank/source 等）、完整 `model_input` 字符串；另以确定性 semantic 探针核对完整允许版本 ID 集合及调用参数，不向外部模型或服务发请求。不得只比较 ID 集合、截断输出或排序后比较。
- [ ] 在运行前后计算 fixture 全表逻辑 hash，要求只读运行前后相同；保存 hash 算法、表/行计数及实际值。哈希不一致时该 case 失败并调查，不能把计时结果作为验收证据。
- [ ] 保留 SQLite SQL trace，并分别汇报查询次数/类型、方法调用次数、新 batch 方法的实际分批次数、返回文档行数和 KB-ID 行数。明确每项计数的来源和统计范围；repository 方法返回列表长度不是 SQLite 全部读取行数，不能标为总加载量。对新旧计数口径变化逐项说明。
- [ ] 保存每个原始计时样本、每轮次序、worker、预热/测量阶段、完整分布统计、中位数及最大值；不丢弃尾部、最大值、异常值、失败样本或资源检查点。失败/超时/中断、资源门禁拒绝及 partial 输出均作为验收记录保留，不得只报告成功样本。

## 判定与归档

- [ ] 将组件旧新性能比较与业务端到端 SLO 判定分别记录。组件比较开始前，记录本次比较的目标/期望、可接受回退条件、代表性负载和尾部统计口径；按这些预先约定的组件标准判断比较结果，不要求先定义业务端到端 SLO。业务方尚未定义的业务 SLO 继续标为待定义；不得自行补造业务 SLO 或阈值，不得把组件比较结果表述为业务 SLO 已达成，也不得把查询次数减少等同于性能验收通过。
- [ ] 完整性门槛：源码与依赖冻结匹配；所有要求 case/轮次/样本完整；输出和 semantic 探针逐项等价；数据库 hash 前后相同；SQL/方法/batch 计数可解释；资源门槛全过程满足；无外部服务或业务数据访问。任何条件不满足则结论标为未通过或未完成，并说明具体原因。
- [ ] 结果报告分别给出每个负载/并发/场景的新旧耗时分布、最大值、计数与资源观测，以及失败和尾部情况；结论限于本次冻结版本、服务器和 fixture，不外推为普适容量保证。只有按预先记录的组件比较标准完成判定后，才能标记该次组件性能比较结果；业务 SLO 是否达成须另行判断，未定义时保持待定义。
- [ ] 归档原始 JSON/日志/SQL trace、fixture 生成参数及 hash、全表 hash、环境与依赖清单、资源检查点、完整样本、异常和最终报告；标明开始/结束时间、执行人、冻结 commit 和工具哈希。历史结果只读保留，不覆盖或续写旧 run。

## 历史证据边界

旧 `b50af49` 相关运行曾因工作站资源门禁停止，只能说明当时未完成矩阵。它不是修复后版本的性能测量，也不能与后续结果拼接或据此宣称优化收益。旧 helper 的 Windows 资源采样行为和旧输出文件应作为历史证据保留；目标服务器上的平台适配与修复后配对测量需作为新的、独立冻结的 run。



