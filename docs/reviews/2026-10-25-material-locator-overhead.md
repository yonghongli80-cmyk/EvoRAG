# 材料定位全量开销优化：实现与隔离验证

状态：实现、独立分项复审、整分支复审和正确性验收通过；正式配对性能矩阵为 **deferred_resource，尚未验收**。源码冻结 `b50af49c3691da16e31d7e5d0d4bded2321e9f11`，保留在本地 `codex/material-locator-overhead-2026-10-25`，尚未再次合并、推送或部署。先前四专项已合并双推，`main`、Gitee、GitHub 均为 `542a36f2ba62ab4ad7f24a7617620040ae3593a3`，本轮末再次只读核对一致。

## 实现及取舍

- 新增按 KB 的真实 SQL 授权投影，在转为 Python 文档前排除无权行。legacy 保留 owner/public/shared 的原名称集合语义、同名行为和 BINARY 比较；组织 READ 判定仍先于 admin。KB 顺序来自未过滤 live/current 行的 `MIN(id)`，KB 内仍按 id 排列。
- 默认定位路径直接消费授权迭代结果，省去完整 visible 字典的再次复制、`list(items)` 和逐文档 versions 小字典；无语义检索时省去未使用的允许 ID 集合，subject 为空时省去无效 exact 扫描。完整 eager `model_input`、有语义检索时的完整版本 ID 集合、全局 FTS top_k、exact→FTS→semantic 顺序和去重保持。
- 构造注入、实例覆盖和模块 callback patch 保持；custom 路径仍在过滤前完成旧版本快照/fallback，保留错误顺序。原全量 reader 和 organizer 行为保持，没有 schema、索引、依赖、缓存或 HTTP/API 变更。
- 所有枚举 KB 都获 full 授权时复用原批量 reader，只返回已判定的 KB；混合允许/拒绝时只查询允许 KB。该分支经过真实 RED 和修复增量复审。普通授权路径按 KB 查询，不能宣称所有分布都减少 SQL；管理员批量路径仍会加载全部获授权行。完整诊断字符串和最终可见文档集合仍是线性成本，custom 路径保留兼容分配。

采用“计划→三个并行 Luna 范围→独立分项复审→原实现者修复及增量复审→一次整分支复审→root 隔离验收”。Task1 的混合 org 授权读取、Task3 的门禁前落盘/权限拒绝证据/计数口径问题均经修复复审关闭。整分支复审未发现确定的 P0/P1/P2；该结论不替代性能或生产验收。

## 已取得的验证

| 检查 | 实际结果 | 范围 |
|---|---|---|
| root 十文件统一回归 | 84 passed，109.30 秒，实际退出 0 | JUnit 84 项，0 失败、0 错误、0 跳过；locator、批量访问、FTS、organizer、org、文档安全边界及三个新增文件 |
| 生产依赖锁的 Python API 烟测 | 4 项通过，0 失败，实际退出 0 | 真实 UserDB、旧新完整可见映射/结果/model_input/semantic 参数、custom fallback/subject=0、同一 locator 上撤销共享后重新鉴权；不含 HTTP/模型 |
| 静态检查 | Ruff 六文件退出 0，四文件 format check 退出 0 | repository 原有整文件格式漂移不混改，新增 repository 范围已在 Task1 检查；Task3 Ruff/compile 退出 0 |
| 冻结旧源/完整等价 | 统一回归通过 | tracked 旧实际 locator 与 golden，不依赖 ignored artifact 或完整 Git 历史；比较有序全字段、完整字符串、FTS 参数及完整 semantic 集合，不只比较 ID |
| 脚本资源停止路径 | 实际 N20 部分运行及显式非性能模拟验证 | 已完成 round 在门禁前落盘，停止后保留 sample、SQL 与按方法/阶段计数；不把模拟数据当性能 |

旧实际 source 的 LF 标准化 SHA256 为 `54593297c81a31000dee9c26336db3198bc85e3e334f001cddb7f33766434bfc`；正式尝试使用 helper 原始 SHA256 `cc02456d0669806d424ff82971f2d77b89e3fb774a0f7937e02dd1c2288daf68`。冻结清单同时绑定产品/测试 Git 对象和新 locator SHA；三次尝试前后产品及 helper 均未变化。golden 曾因合成 fixture 的激活时间变化而失败，随后固定合成时间并保留 updated_at 比较，重新从旧源生成一次后，无再生成检查通过。

## 性能矩阵：资源门禁未通过

在 pytest 结束、生产锁烟测结束后，root 才启动同一冻结源码的正式配对命令：N=1000/5000/10000、C=1/2、三轮，每 worker 三次预热和十次测量，另含 many-KB/admin 的 N≤100 小场景。预期 18 条 case 记录，其中六个主档、十二个按 requested_N 标记的重复小场景。各轮是旧/新整批 AB/BA，不混合旧新实现并发。使用真实 SQLite、完整输出与 model_input 对照、离线网络保护、BelowNormal、BLAS=1；所有数据库路径限定在新建的合成 run 目录，排除 dotenv source。

| 尝试 | 实际退出 | 保留状态与原因 |
|---|---:|---|
| 1 | 2 | initial 门禁：free 962,703,360 B，未生成测量样本 |
| 2 | 2 | N1000/C1 完成第一轮十对样本后停止；free 1,049,972,736 B，WS 109,842,432 B，保留完整 round/sample/SQL/计数快照 |
| 3 | 2 | initial 门禁：free 1,034,264,576 B，未生成测量样本 |

三次均低于 free≥1 GiB 的既定保护门槛。没有降低门槛、停止用户程序、与 pytest 重叠或调用业务服务压测。第二次十对样本不构成正式矩阵，也未完成该 case 的最终全表 hash 对照；不据此计算验收 P99、宣称耗时/内存下降或完成性能验收。此前旧容量实验的 2520 样本也不作本次配对 baseline。

计数明确分开 material-document rows 和 KB-ID rows，并按 diagnostic_probe/warmup/timed_measurement 保存。它只统计三个被包装 repository 方法的返回列表长度，**不是全部 SQLite 行加载数**；完整 SQL trace 单独保留。检查点资源是软门禁，不是操作系统硬内存限制。

## 运行保护与后续

22:03 原 8080 服务仍为 PID48512、启动时间 2026-10-23T14:09:17.6870660+08:00，ready 200、SQLite/Qdrant local 正常、Redis optional degraded；D `.env` 仅核对哈希且保持，D main 为 `542a36f`、跟踪树干净。未重启、reload、部署、执行业务恢复/迁移或读取业务数据库及材料。结束时另保存最终服务核对。

源码正确性门禁已通过。下一步是在资源充足的隔离时段执行完整新配对运行，保留既定门槛和全部样本；若结果不佳，按分段/SQL/计数定位，再做同负载对照。完整矩阵通过及代价审查前，不将本分支作为已完成性能验收的版本交付 main。

保留的 C 工作树可执行：

```powershell
# 沿用本次无 .env 工作树和私有解释器；先确认没有 pytest/其它本专项负载运行
& .\artifacts\material-locator-optimization-2026-10-25\verification\run-paired-benchmark.ps1 -Attempt 4
```

该命令创建新证据和 fixture，不覆盖前三次或续拼部分结果，仍执行源码/helper 冻结校验。具体计划见[实施计划](../superpowers/plans/2026-10-25-material-locator-overhead.md)，设计/裁决见[设计文档](../superpowers/specs/2026-10-25-material-locator-overhead-design.md)。原始日志、JSON、JUnit、脚本和分项/最终复审在本工作树 ignored `artifacts/material-locator-optimization-2026-10-25/` 与专属 `.superpowers/sdd/2026-10-25-material-locator-overhead/`，保留原件。公司工作记录在实际 submit 后补回执，不预填成功。
