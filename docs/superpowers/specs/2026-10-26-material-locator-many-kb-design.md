# 材料定位多 KB 查询修复设计

本设计补充 2026-10-25 材料定位设计，不改变原权限/完整输出契约。用户 2026-10-26 指示继续剩余步骤，并明确压测留待服务器，本轮将性能验收标记为未执行/待验收，不再启动本机压力矩阵。

## 证据与选择

冻结 b50af49 的正式 Attempt4 因 free-memory 门禁退出 2，完成 N1000/C1 两 KB 和 N100/C1 多 KB 两条记录各 30 对，admin 小场景另保留一轮 10 对。两 KB 中位 12.94275→11.73570ms，但新实现出现 81.7262ms 最大值；多 KB 中位 2.9732→4.41565ms，三轮中位均退化。样本不能证明稳定生产性能，也不能忽略退化。

实际 trace 与只读 EXPLAIN 表明，多 KB 新路径每个 KB 重复执行完整 document_permissions/document_versions join 和 ACL 名称子查询；旧路径一次完整 join，之后每 KB 只做轻量名称查询。故修复对象是每 KB 重复宽查询，不按计时阈值写易抖动测试。

root 选择有序授权计划 CTE 的分批 SQL 投影：每批一次完整 join，并在 SQL 内按 (KB, BINARY 名称) 过滤 legacy ACL。备选逐 KB UNION ALL 虽能复用谓词，但保留重复 join/子查询和 compound-select 限制，不选择。此为 root 在已有“效果不好则优化”和“先做”授权范围内的实现裁决；代价是增加内部批量接口及 SQL 复杂度，须有真实 SQLite 权限/顺序/参数上限回归。

## 接口与边界

新增 `MaterialRepository.list_material_documents_for_access_plan(access_plan: Sequence[tuple[str, bool]], *, user_id: int | None) -> list[dict[str, Any]]`。

`access_plan` 按旧未过滤 live/current KB 的 MIN(id) 顺序，只包含本次已判定可读取的 KB；第二项为 apply_legacy_acl，False 仅用于已获 full 授权的 KB。不得通过 user_id=None 推断 full。内部将每项变为 (kb_id, apply_legacy_acl, ordinal) 绑定参数；所有值使用参数，不拼接标识或用户输入。

每批最多 300 KB，且动态遵守当前连接 SQLite variable limit：三项/KB 加两个 user 参数，按 `(limit-2)//3` 计算实际批量大小；不足一项时明确失败，不超限尝试。空计划立即返回空列表且不执行文档 SELECT。计划连续切片，各批 SQL ORDER BY ordinal, dp.id，拼接保持 KB 及文档顺序。字段/列顺序与旧完整 reader 相同。

legacy 允许名称来自原 owner/public/shared（view/edit 都包含）的名称投影；按 KB 与 BINARY 名称配对，保留同 KB 同名跨行来源，包括删除/无当前版本来源的原语义；不能把跨 KB 同名串联授权。不新增 permission_level 或来源 deleted 条件。None 用户仍只能从 public 名称得到 legacy 许可。组织授权继续先于 admin，拒绝 KB 不进入计划，无权文档不能转换为 Python 文档行。

iterator 对所有枚举 KB 都 full 的情况继续复用旧批量 reader，并过滤未判定 KB。只有一个获准 KB 时保留原显式按 KB reader；两个及以上获准 KB 使用新批量计划 reader，直接按返回顺序 yield。原两个 repository 接口、旧 full reader、organizer、locator custom/fallback、model_input、semantic 集合、过滤和 FTS 行为保持。无 schema、索引、依赖、缓存或 API 变化。

## 验收与保护

真实 RED 必须证明 12 个交错插入 KB 在本轮旧实现上仍触发 12 次宽查询，而新方案只按批次数查询；同时断言完整行/顺序和实际 fetch 文档行均已授权。测试覆盖跨 KB 同名、组织允许/拒绝及 admin 优先级、同 KB 名称来源、None 用户、空计划、混合模式及低 SQLite bind limit 下的连续分批。

先 RED，再最小实现，再 focused GREEN、独立规格/质量复审和 root 相关统一回归。生产依赖锁的合成 Python API 烟测在资源允许时运行。沿用原冻结旧 source/golden，不重生成期望或削弱现有断言。

产品/测试只在 C 隔离分支；D main、两个远端和原 8080 不切换。离线/BelowNormal/BLAS1、一个 pytest worker、新短 Temp；不启动压测、真实模型或访问业务 DB/.env。服务器配对性能验收是显式空缺，不用查询数量改善代替耗时、长尾或容量承诺。
