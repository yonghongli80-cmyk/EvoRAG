# Material Locator Many-KB Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Root owns design, Git, recorder and integration; Luna owns scoped implementation and independent review.

**当前状态：** 受测冻结7200e30；独立增量复审、十一文件92通过/0跳过（123.91秒）及生产锁4项烟测通过。按用户要求修复后压测空缺，留待服务器；源码仍本地。详见[验收与空缺](../../reviews/2026-10-26-material-locator-many-kb.md)。

**Goal:** 修复实际发现的多 KB 重复宽查询开销，保留原权限和完整定位行为；压测明确留待服务器。
**Architecture:** 有序授权计划 CTE 分批投影；所有 KB full 时保留旧 bulk，单获准 KB 保留原显式 reader，多获准 KB 消费新批量 reader。
**Tech Stack:** 当前 Python3.12/SQLite/UserDB/pytest/Ruff，不新增依赖。
**Spec:** `docs/superpowers/specs/2026-10-26-material-locator-many-kb-design.md`；原设计所有兼容不变量继续生效。

## Global Constraints

- 起点 HEAD a59a241，产品冻结 b50af49；main/Gitee/GitHub542a36f。C 原隔离分支继续，不在 D checkout、不 merge/push/deploy/restart，不读业务 DB/资料或真实 .env。
- root timer T-990C8E3E458E，原任务记录不重提；原证据/环境/工作树/专属 SDD 保留。新证据在 artifacts/material-locator-acceptance-2026-10-26。
- 新内部接口、300 KB 上限、实际 bind limit、参数化计划、name/KB配对、None/public、org先admin及排序均以设计为准；旧 reader/organizer/locator/custom/model_input/semantic/FTS/schema/依赖保持。
- Python `artifacts/review-backlog-2026-10-24/env/developer-python/Scripts/python.exe`，runtime 为同 env/runtime-python。PYTHONPATH 明确 repo + kb-lifecycle-concurrency-2026-10-24/tools + staging-recovery-2026-10-25/verification/tools；offline_network_guard/staging_test_priority、BLAS1、BelowNormal、一个 worker、独立短 basetemp，不改 TEMP。
- 子代理不 Git 写/recorder/派代理，不启动压力矩阵；所有 exec 显式 C workdir。root 根据宿主资源串行安排测试，不与任何压测叠加。

## Review Focus

- 计划只含获授权 KB，False 模式不能由 None 用户或 org 拒绝变出。
- 同 KB 名称来源和跨 KB 同名隔离、BINARY 大小写/Unicode 与缺用户保持。
- interleaved KB id 顺序、chunk 边界及 current/deleted 条件完整不变。
- CTE 子查询在 SQL 中过滤，无权宽行不落 Python；实际查询数量与 fetch 行证据不能用 mock ACL 冒充。
- 参数上限过低/空计划、bind 重复次数及旧 all-full/单 KB 兼容不能超限或隐式 full。

### Task 1: 批量授权计划与真实回归

**Files:** Modify rag/repositories/material_repo.py、rag/materials/locator_visibility.py；Create tests/test_material_visibility_batch_plan.py。原 source fixture/golden 禁改。

- [x] 先创建真实 SQLite 12 交错 KB/完整行/顺序/隐藏行及查询计数 RED；运行覆盖测试并保存真实失败，再写实现。
- [x] 新接口/CTE/连续分批/bind limit；iterator 按设计选择 bulk/single/batch 路径。补跨 KB 同名、同 KB 来源、None/public、空计划、org混合及低 limit 测试。
- [x] 串行新文件+原 projection/batched/locator-equivalence 回归，Ruff/变更范围 format，自审报告给 root。原接口继续测试，无测试断言弱化。

### Task 2: 独立增量审查与统一正确性验收

- [x] root 保存完整 diff/commit，独立 Luna 只审本次规格和质量，问题由原实现者修复并增量复审。
- [x] 受测源码冻结后 root 十一个相关文件统一回归（原十个+新 batch plan），核对 JUnit/实际 exit；生产锁合成接口烟测四项。仅产品/测试改动才触发这次新回归。
- [x] 增量终审覆盖本次修改，不重复已接受旧分支整审；记录原服务前后 PID/start/ready/env hash、D main及两远端状态。

### Task 3: 服务器性能门禁与交接

- [x] 更新交接/索引/容量文档：旧 Attempt4 是发现信号，修复后压测为“未执行/待服务器验收”；不得比较不同版本 partial 后宣称收益。
- [x] 保留旧不可变 helper/结果；写服务器后续测试清单，包含相同三轮配对、样本/最大值/完整等价/hash、many-KB、admin及隐藏资料分布，固定硬件/版本/资源门槛。
- [x] **R-20261026-B9CF2494 实际 submit 成功。** 实际公司 submit 一条本事项记录，检查成功并补回执；归档非 DB 证据，不自动再次合并/部署。

**Preflight:** T1 product/repo/iterator 三个文件由原 SQL Luna 单独负责；现有接受 source/golden 不动。T2 root tests等待T1代码冻结，review只读可并行但不运行负载。Task3仅docs/root，基于实际验证结果收尾。原复审已完成部分不重做。
