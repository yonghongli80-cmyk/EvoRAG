# Staging Audit Jobs Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** 消除暂存审计逐版本重扫全部jobs，同时保持完整报告和只读安全契约。
**Architecture:** 在现有jobs完整校验后按version预索引，诊断阶段查对应桶。产品/复杂度测试、独立兼容golden和benchmark准备文件互不重叠，可并行；root统一Git及最后冻结验收。
**Tech Stack:** Python3.12、stdlib SQLite/JSON、pytest、Ruff，复用已有开发及生产锁环境。
**Spec:** `docs/superpowers/specs/2026-10-25-staging-audit-jobs-index-design.md`。

## Global Constraints

- 基线`9a8653c1e4fc51d462268aa8c9fb54403a972720`，复用C隔离工作树；不碰D主目录代码、业务库/向量、真实.env、运行服务、模型或Docker。
- 仅新增内部`material_jobs_by_version: dict[int,list[dict]]`；既有SQL、完整校验、`jobs`列表、报告值/顺序和所有只读/路径/身份保护保持。
- 过滤原样保留scope/type的缺列默认与显式NULL区别；所有jobs先校验，包括非material及孤儿行。
- 新索引增加O(J)引用；复杂度验收计数，不设时间阈值，不宣称整个审计线性或生产SLO。
- 子代理用Luna，不派生子代理、不做Git、不启动timer/提交记录。root唯一timer`T-DE8458BE97A6`，保留全部证据和环境。
- pytest须先加载offline_network_guard及staging_test_priority，原生低优先级、BLAS1、短且全新的系统Temp。每路定向单进程；统一gate至多2 worker。

## Review Focus

- jobs表或scope/type列缺失，与显式NULL/空串的结果不同：Task2 golden及参数测试。
- 非material/无对应版本的非法jobs仍在任何诊断前报unsupported_schema：Task2完整错误报告和输入不变断言。
- 同version多job、反向插入id和混合状态保持statuses/pending job_ids顺序：Task2完整报告golden。
- 只优化索引却仍在每个版本遍历全局jobs或重复SQL：Task1真实装载路径的计数RED/GREEN。
- 输出/文件安全被性能修改带偏：Task2快照/目录前后hash，root现有CLI、链接、WAL、恢复集成回归及旧新完整报告对照。

## Environment and evidence

开发PY=`artifacts/review-backlog-2026-10-24/env/developer-python/Scripts/python.exe`；生产PY为同env下`runtime-python/Scripts/python.exe`。PYTHONPATH含repo、`artifacts/kb-lifecycle-concurrency-2026-10-24/tools`和`artifacts/staging-recovery-2026-10-25/verification/tools`；统一xdist另加已有test-tools。所有原始日志位于`artifacts/staging-audit-jobs-index-2026-10-25/`，SDD只使用本计划专属目录。原冻结模块由root通过Git字节导出供golden/benchmark生成使用，永久测试不得依赖Git或ignored artifacts。

### Task 1: Pre-index material jobs and prove bounded traversal

**Files:** Modify `rag/staging_audit.py`；Create `tests/test_staging_audit_jobs_index.py`。
**Interfaces:** 原`audit_staging`不变；`_collect_database_references`产出`material_jobs_by_version`内部键，`_diagnose_version`消费其version桶；无新公共helper要求。

- [x] 新计数回归经真实`audit_staging`/合成SQLite/_select_rows路径包装jobs列表（保留原数据），对V=1、12、48且含多job/孤儿/非material行的情形，断言jobs只装载一次、完整行遍历不超过校验加索引两遍；V=12/48在旧实现必须因重复访问RED。不得以stub直接返回预制报告或以时间作门槛。
- [x] 最小实现：完整校验之后建立有序桶，原行引用及原jobs列表保持；诊断仅查桶，保持既有状态/输出逻辑，避免无关重构及增加依赖。
- [x] 定向GREEN，Ruff/format/compile及diff通过，写task-1-report.md，列实际命令、RED/GREEN计数和风险；root选择性提交，fresh规格/质量复审。

### Task 2: Independent full-report and schema compatibility coverage

**Files:** Create `tests/test_staging_audit_jobs_compatibility.py`、`tests/fixtures/staging_audit_jobs_expected.json`。
**Interfaces:** 仅消费公共`audit_staging`，用root导出的基线模块生成golden；最终测试只依赖仓库fixture和临时合成快照，不依赖基线模块/源代码/Git/artifacts。

- [x] 准备四类合成golden：jobs表缺失；只有id/version_id/status的最小表；完整scope/type列含NULL/空串/大小写/非material；多版本多job混合活跃/终态/缺失与孤儿。反向插入job id，覆盖报告statuses及pending job_ids排序；至少一例含既有KB引用、manifest及stage文件以锁住完整报告阶段顺序。
- [x] 使用冻结基线生成并核对完整报告golden，仅递归规范临时绝对路径前缀为`<TMP>`（路径后缀用posix表示），不删字段/条目、不排序输出。基线必须先通过，明确这是等价守卫，不宣称它有初始RED。
- [x] 对jobs缺必需列及非法正整数id/version_id、非法status、非文本scope/type参数化，包含非material和孤儿行，断言完整unsupported_schema错误、无部分诊断；每例输入数据库hash和树快照保持。
- [x] 在当前实现运行新文件GREEN，Ruff/format/compile，写task-2-report.md含golden来源及旧/新实测结果；root提交及fresh规格/质量复审。若Task1尚未落地，可先验证旧版，最终统一gate验证新旧等价。

### Task 3: Prepare reproducible old/new synthetic measurement

**Files:** Create ignored `artifacts/staging-audit-jobs-index-2026-10-25/benchmark_jobs.py`与本计划`task-3-report.md`；不改产品/测试/依赖文件。
**Interfaces:** 通过相同显式合成快照分别调用冻结基线和当前`audit_staging`；脚本先导入offline/priority guard，不导入应用config。

- [x] 准备N=100/500/1000（每版本含material job，另含孤儿/非material干扰）旧/新对照脚本，同一快照和目录顺序串行执行、交替old/new各3次，输入hash前后不变，完整报告逐值相等。
- [x] 记录每轮耗时、tracemalloc峰值、报告bytes/entries/findings、Python版本/实际优先级、base源码hash及参数。峰值和时间均为诊断，不设通过阈值或推断生产SLO。只compile检查，不在源未冻结时执行benchmark。
- [x] 写准备报告；root代码冻结和复审通过后原生低优先级执行，保留stdout/stderr/退出码及JSON结果。

### Task 4: Root integration, review and delivery

**Files:** Update本计划、`docs/staging-audit.md`、`docs/README.md`、`docs/handoff-2026-10-24.md`；Create `docs/reviews/2026-10-25-staging-audit-jobs-index.md`。

- [x] root核对三路文件与接口，Task1/2独立复审及整分支复查；有实际发现则一个最终修复波次和一次增量复查，冻结产品/测试。
- [x] 原生统一运行`tests/test_staging_audit.py tests/test_staging_audit_jobs_index.py tests/test_staging_audit_jobs_compatibility.py tests/test_staging_recovery_integration.py tests/test_architecture_budget.py`，保存退出码/stdout/stderr/JUnit；全树Ruff及新文件format/compile。只审计相关范围，不把旧全后端基线计入本轮数字。
- [x] 执行Task3 benchmark及生产锁CLI smoke（临时快照、guard、原JSON/退出码/只读边界），核对旧新完整报告相等及输入不变；只读核对原服务PID/start/ready/.env hash。
- [x] 更新验收报告、交接和索引，记录精确计数/测量及局限、root所有裁决；选择性提交，实际submit当前唯一中文公司记录并核对成功。保留隔离分支和原始证据，不自动合并/双推/部署。

最终统一验收：冻结e52237c、五文件45通过0跳过、26.34秒/退出0，JUnit45项0失败0错误；Ruff/新测试format通过，生产锁CLI4项通过。旧新18次对照完整报告一致、输入不变；N1000/J3000中位6.787→5.885s，仅合成诊断。15:13:20原服务身份、ready200和环境哈希保持。独立复审及整分支复查批准、两P2测试缺口已关闭，详见验收报告。

根收尾：验收文档提交`3c609b9`；唯一公司记录实际submit成功，返回`R-20261025-56747D6E`、`pending_review`，等待人工复核。本计划全部完成，后续仅补回执；工作树、SDD、私有环境及原始日志保留。
