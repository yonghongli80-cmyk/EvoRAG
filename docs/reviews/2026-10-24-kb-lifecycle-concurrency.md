# KB 删除与上传并发一致性整改

日期：2026-10-24
代码分支：`codex/kb-lifecycle-concurrency-2026-10-24`
实现基线：`9f0574bc4ed513b9099db1cd428255f584f4b3fb`
实现提交：`a87451677b74953b2d4d2023f4e9454b24bfea3f`
计划与设计：[实施计划](../superpowers/plans/2026-10-24-kb-lifecycle-concurrency.md)、[设计规格](../superpowers/specs/2026-10-24-kb-lifecycle-concurrency-design.md)。

## 修复范围

- 删除完成后 metadata 已删除但 tombstone 保留时，edit/admin/再次 DELETE 返回 `404` 和 `X-KB-Lifecycle-Error`；无 lifecycle 行的旧 metadata orphan 仍保留 `409`。
- Wiki API lifecycle exceptions 统一使用共享 HTTP mapper，保持 detail、status 和 machine header 一致。
- 抽出同步 KB 删除协调器供 HTTP 路由与集成测试共用。自有子进程在真实清理后崩溃，重试经同一 API 协调器完成；检查 scoped jobs、FTS/fingerprint、文件、Qdrant、保留 token 和另一 KB。
- 已存在且无法证明属于目标 KB staging root 的归档源 fail-closed，返回 `503 archive_source_unowned`，保留源文件与manifest供后续人工处理。
- 把 `private` scope 与 `kb_shares` 的校验移入 KB gate 和同一 SQLite immediate transaction，避免并发共享关系被scope更新顺带删除。
- 修正 promotion 失败后 staging 被无条件删除的既有数据保留回归：只在 `material_promotion_failed` 时保留暂存字节，成功、生命周期、授权及其他错误仍按既有路径清理。
- 更新过时的共享错误故障注入，并隔离 helper 测试对 `sys.path` 的改动；架构 ratchet 的局部导入数恢复到 150。

## 复核

Fresh Luna 对集成 diff 作只读审查并批准；没有 Critical、Important 或 Minor findings。审查覆盖本报告列出的生命周期状态映射、协调器崩溃/重试、unowned archive、share竞态、promotion暂存保留、测试隔离和架构预算。Wiki page 的 KB 归属仍由 `WikiStore.wiki_pages.kb_id` 持有。

一项早期 Minor 建议要求 orphan metadata 的 409 回归断言；既有 `tests/test_kb_security_boundaries.py::test_missing_metadata_is_conflict_for_mutation` 已直接断言此行为，并纳入最终完整后端套件，因此没有增加重复测试。

## 验证

### 新缺陷回归

- 新增五个行为回归先得到 `6 failed in 21.62s`，包含 Wiki busy/deleted 两个参数化案例；失败均对应预期缺口。
- 修复后同组 `6 passed in 21.05s`。
- 相关 lifecycle/API/cleanup/权限套件 `139 passed, 1 skipped`；此前已处理的授权重检与 stale operation 回归 `2 passed`。
- 共享错误注入、helper `sys.path` 隔离及 runner path 行为定向验证 `3 passed in 9.06s`。
- promotion失败保留测试 `1 passed in 8.44s`，相关材料备份测试文件 `3 passed in 8.77s`。
- 架构门禁 `2 passed in 8.82s`；全树局部导入计数 150。

### 最终统一后端

在 C 隔离 worktree 使用锁定开发 Python、`offline_network_guard`、`test-tools` 中的 pytest-xdist、系统 Temp basetemp `C:\Users\Syf15\AppData\Local\Temp\kb24b`、4 worker/loadfile、单线程 BLAS 和 BelowNormal 优先级，一次完整后端回归：

```powershell
$env:PYTHONPATH = "$repo;$guard;$testTools"
$env:PYTHONHASHSEED = '0'
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
$env:NUMEXPR_NUM_THREADS = '1'
& $python -m pytest -p offline_network_guard --basetemp "$env:TEMP\kb24b" tests -q -n 4 --dist loadfile --tb=short -ra --junitxml "$repo\artifacts\kb-lifecycle-concurrency-2026-10-24\task-7\backend-final-short.xml"
```

结果：`3141 passed, 4 skipped in 529.36s (0:08:49)`，退出码 0。JUnit 为 3145 项、0 failures、0 errors、4 skipped，SHA256 `E1DA2D22135AAD6A47514EEE422031E186EF846C42E33C73FBDFE42E40FE008E`。四个 skip 原因：Windows terminate 行为、Windows 不支持 fork、两项真实脱敏表格夹具缺失。

最初诊断运行使用过较长 basetemp；临时路径达到 268 字符，Windows staging/归档用例出现 11 项 `FileNotFoundError`/归档错误。改用短系统Temp目录后，归档 CAS 单测 `1 passed`，完整后端 `3141 passed / 4 skipped`。这次改动只缩短验证临时路径，没有改产品或测试代码。

全树 Ruff：`All checks passed!`，`git diff --check` 通过。生产依赖锁下还在系统 Temp 合成 SQLite 中运行 offline lifecycle smoke，输出 `runtime_lock_smoke=passed terminal_tombstone=passed`。

### 运行与数据边界

验证仅在 C 隔离 worktree、系统临时目录和合成数据库/向量数据执行；启动离线网络守卫，未访问真实服务端口、`.env`、业务数据库或业务向量数据，未调用模型或 Docker。D 主工作树中 PID 48512 的服务只读检查保持 `/ready` 200，`.env` SHA256 `6128C4BD1F1BC0E43AB5189BAD25FA160CB8567B555B1B0CC5921E8771B0E1BD`，无服务重启或存储切换。

## 剩余边界与交付

- 本次提交未应用到 D 主工作树的运行实例，不能据此声称业务库已启用 lifecycle gate，也没有回填或清理既有孤儿路径。
- 同库跨进程互斥保证仍要求各进程使用相同规范化绝对 DB 路径和可靠共享文件系统锁；`:memory:` 只在其 ConnectionManager 内互斥。路径别名/硬链接和不可靠共享文件系统不在保证范围。部署前应备份、停写并排空旧版本写者，再统一升级所有可能写KB的进程；不支持新gate/tombstone的写者不得与新版本并行运行。本轮未安排该业务停写窗口。DELETE worker 会在gate内重新检查administer权限，排队期间撤权返回403且不开始删除。
- Promotion 失败按现有契约保留无文档身份的 staging 文件；后续需由重试或人工/清理流程处置。无法证明归属的 archive 源则连同 manifest 留存，需人工解析后重试。
- 2026-10-25按用户授权，将已验收分支快进并入main（`9f0574b` → `d5ff13e`），并推送Gitee/GitHub；两个远端主分支及全部标签实际核对一致。运行服务未部署或重启。

## 2026-10-25 合并与双远端交付

- 合并为fast-forward，main源码/测试与已完整验收的`a874516`一致；其后只有文档变化。合并后在无业务配置的C隔离worktree执行22项生命周期、架构、真实协调器崩溃重试及promotion保留冒烟，`22 passed in 44.47s`、退出0。
- 交接四条push命令全部成功；首次核对Gitee/GitHub main均为`d5ff13e2826bf61a502cfb0dc7d5934419384654`，两个本地tag refs与两端零差异，无强推。后续本段交付说明为纯文档收尾，最终SHA以本地main及两端ls-remote为准。
- 原PID48512及启动时间保持，`/ready`200、Qdrant local、Redis optional degraded，`.env`哈希保持。没有业务停写、数据迁移或服务重启。
- 交付证据保留在C隔离worktree的`artifacts/kb-lifecycle-concurrency-2026-10-24/delivery/`，包括冒烟日志和真实ref/tag核对JSON；工作树与私有环境保留供后续工作使用。
