# 材料定位多 KB 批量查询整改与交接

后续用户已授权源码合并双推，当前交付状态见[交付报告](2026-10-26-material-locator-delivery.md)；下方本地分支状态为本专项结项时的快照，性能空缺继续有效。
**状态：本轮代码、独立增量复审和正确性验收完成。修复后的服务器压测未执行，性能结果留空。** 用户明确后续部署服务器再压测；本轮不再运行本机压力矩阵。受测源码冻结 `7200e3040e77355c5859dfb5ea03383275301d5e`，仍在本地 `codex/material-locator-overhead-2026-10-25`，未再次合并、双推或部署；已交付 main 仍为 `542a36f`。

## 发现与修复

继续原计划时先运行冻结 b50af49 的 Attempt4，随后由独立 Luna 复算原始数据。该运行因 free-memory 门禁退出 2：完成两个场景各 30 对样本，admin 小场景另有 10 对 partial，共保留 70 对，未完成 18 条 case/810 对的矩阵。两条完整 case 的全表逻辑 hash 前后一致；partial 没有最终 hash，不能算完整验收。

旧双 KB N1000/C1 样本中位 12.94275→11.73570ms，但新路径最大值 81.7262ms，不能宣称尾部改善；many-KB 实际 N100/C1 中位 2.9732→4.41565ms，三轮各自中位均退化。这是 **修复前版本的诊断信号**，不是本次修复后的测量。用户随后明确压测留待服务器，停止新增本机压力运行。

实际 SQL trace 与只读 EXPLAIN 指向逐 KB 重复宽字段版本 join 和 ACL 名称子查询：100 个 KB 的新路径执行 100 次完整投影，旧路径一次完整投影后再逐 KB 查轻量名称。root 选择有序授权计划 CTE，Luna 实现分批投影；未采用仍保留重复 join 的 UNION ALL 方案。

- 新内部 repository 方法接收本次已经授权的 `(kb_id, apply_legacy_acl)` 计划。每批最多 300 KB，并按实际 SQLite variable limit 计算 `min(300, (limit-2)//3)`；三绑定/KB、两绑定/user，所有值参数化。空计划不查文档，limit 不足五时在宽查询前明确失败。
- SQL 中按 **KB 与 BINARY 名称配对**保留 legacy owner/public/shared 的名称集合行为，包括同 KB 删除/无当前版本来源；跨 KB 同名不串联。None 用户保持 public 名称规则，org READ 仍先于 admin，拒绝 KB 不进计划。
- 连续分批、每批按 ordinal/id 排序，保持旧 MIN(id) 的 KB 分组与组内 id 顺序。无权宽行不转换为 Python 文档。所有枚举 KB full 时仍走原 bulk 且过滤未判定 KB；一个获准 KB 保留旧按 KB reader，多个获准 KB 才使用新方法。
- 原完整 reader、organizer、locator/custom fallback、完整 model_input/semantic ID 集合、FTS 行为、schema、索引和依赖均未改。本轮只改 repository/visibility 和新增回归测试。

真实 SQLite 的确定性回归证明：12 KB 的宽查询由 12 次变为单批 1 次，并检查完整行、实际 fetch 文档 ID、隐藏行排除与交错 KB 顺序。这个查询数量变化不等同于耗时、长尾或容量验收通过；分批数还受 KB 数量和连接 bind limit 影响。

## 正确性验收

| 检查 | 实际结果 |
|---|---|
| 修复前 RED | 新测试在产品修改前因 12 次宽查询不满足一次批量查询契约失败，真实退出 1 |
| root 十一个相关文件统一回归 | **92 passed，123.91 秒，实际退出 0**；JUnit 92 项，0 失败、0 错误、0 跳过 |
| 生产依赖锁合成 Python API 烟测 | **4 项通过，0 失败，实际退出 0**；真实 UserDB/旧新完整映射和结果、model_input/semantic 参数、custom fallback/subject=0、共享撤销后的重新鉴权；不含 HTTP/模型 |
| 静态检查 | Ruff 三文件、两文件 format 及 repository 变更范围 format 均退出 0 |
| 独立 Luna 增量规格/质量终审 | 通过，无已确认 P0/P1/P2 或需单列 minor；不重复已接受旧分支整审 |
| 旧 source/golden 与运行环境 | 冻结旧 source/golden、依赖和旧压力 helper 保持，不重生成期望，不用旧 84 项结果冒充新 92 项验收 |

新测试覆盖跨 KB 同名、BINARY 大小写、None/public、同 KB 删除/无版本名称来源、混合组织权限、all-full admin、空计划、低 bind limit、300 KB 批量上限、真正交错文档 id 和连续 chunk 顺序。

过程中的代理仪表/命令路径/SQL 字符串等临时失败均留证。一次 39 项组合检查为 36 通过、3 失败，原因是新增 fixture 将晚插入文档的预期顺序索引写错；修正后这三项定向通过。随后 root 对最终冻结源码完整运行上述 92 项全部通过。原 RED 的 fixture 与后来增强的交错样本分开记录，未倒写失败证据。实现者 basetemp 位于专属 artifact；root 统一验收使用新的 OS 短 Temp，均为隔离数据。

## 性能验收空缺

| 修复后服务器验收项 | 当前结果 |
|---|---|
| 目标服务器/OS/硬件及依赖冻结 | 空缺，未执行 |
| 完整旧新配对矩阵与扩展权限分布 | 空缺，未执行 |
| P50/P95/P99、最大值、吞吐与峰值资源 | 空缺，不填 0 或推测值 |
| 耗时收益、多 KB 退化关闭、稳定长尾与生产容量结论 | 未验收 |
| 业务端到端 SLO | 未验收，与组件比较另列 |

后续使用[服务器验收清单](../material-locator-server-acceptance.md)，冻结整个产品/测试树和 helper，不只核对未改动的 locator.py。旧 helper 为 Windows 专属资源 API；Linux 等目标平台的适配/独立复核仍待执行，不把原工作站命令当作已可跨平台运行。基础配对矩阵为 18 条 case、810 对样本，小场景实际 N100；新增 hidden 分布扩展单独计数，不能与旧 partial 拼接或静默改口径。

## 运行保护与证据

09:43:51 最终检查：原 8080 仍为 PID48512、启动时间 2026-10-23T14:09:17.6870660+08:00，ready 200，配置哈希保持；D main `542a36f`、跟踪树干净。只做必要健康检查和 `.env` 哈希核对，没有读取业务资料/数据库、重启、reload、部署或运行恢复/迁移。

本轮设计、实现和独立复审按[计划](../superpowers/plans/2026-10-26-material-locator-many-kb.md)执行。原始证据在 C 工作树 ignored `artifacts/material-locator-acceptance-2026-10-26/`、专属 `.superpowers/sdd/2026-10-26-material-locator-many-kb/`；Attempt4 原件在旧 artifact/verification，旧运行和原环境均保留。验收文档提交 `f919bfe`；公司记录已实际 submit 成功：`R-20261026-B9CF2494`、`pending_review`，timer `T-990C8E3E458E`，实际退出 0。随后只补回执，受测产品源码仍为 `7200e30`。
