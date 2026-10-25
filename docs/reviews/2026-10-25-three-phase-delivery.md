# 暂存安全、上游观测与jobs索引源码交付（2026-10-25）

状态：第一阶段源码合并及双推已实际完成，首轮交付`0724738ef54843f666fa25e78137d21007ee7676`；后续仅补交付文档，最终SHA以main/两端ls-remote为准。完整集成通过，未执行运行部署；用户授权的第二、三阶段将继续依序推进。

## 交付范围与原状态

本轮fetch后，D main、Gitee origin/main和GitHub github/main均为`3f524904e16b19119fe76009c41763b89bbdae8d`；原本地候选为`2d9102cde11e98902fd087e8206f716141fa607d`，领先34提交且不落后，随后仅新增顺序计划到`6f79042eff0930e0f02a3e76586eb99f32b22cb5`，作为完整集成冻结。

交付包括[材料暂存安全](2026-10-25-staging-safety.md)、[上游响应关联观测](2026-10-25-upstream-observability.md)及[jobs关联索引](2026-10-25-staging-audit-jobs-index.md)三轮已复审成果，源冻结/提交祖先关系及文档等价由Luna只读核对。累计变化不含前端、静态构建、生产/开发依赖锁、pyproject、真实.env、artifacts、本地AGENTS或公司recorder数据；两份用户未跟踪文件保持原状。

总顺序见[实施计划](../superpowers/plans/2026-10-25-sequential-followups.md)。历史分轮验收数字是各自冻结的证据，本次以最终集成单次完整结果为准，不相加。

## 集成与发布验证

统一冻结`6f79042`上，全树Ruff退出0。完整后端在C无.env隔离工作树、原生2 worker/loadfile、BelowNormal、BLAS1、offline guard和新短basetemp运行：**3263通过、4跳过**，861.33秒、退出0；JUnit3267项，0失败、0错误，suite计时861.055秒。跳过为Windows terminate/fork限制及两项缺失真实脱敏夹具，原日志和独立汇总保存于`verification/integration-summary.json`。冻结后的产品/测试/锁文件无漂移。

推送前再次fetch确认两端未前进，15:47:27将D main从`3f52490`快进到`0724738`，与候选tree同为`00859313fc0d3714f8862980807bdc3a7f136805`；和完整受测冻结的差异仅文档。合并后在同一无.env隔离源码树实际执行6项定向烟测，18.94秒/退出0；生产锁另执行SDK9项（5.672秒）及实际CLI4项，均退出0，客户端传输、JSON/stream/重试、缺头/错误及只读CLI边界保持。未在含真实.env的D主目录运行测试。

按交接依次执行`git push origin main`、`git push github main`、`git push origin --tags`、`git push github --tags`，均成功；15:49:27实际ls-remote确认两端main=`0724738`，全部tag refs与本地一致。推送均为fast-forward，无force、无标签值修改，原分支/工作树保留。后续回执文档同步仍保持受测源等价。

## 证据归档

Luna审计核对三轮原清单60个artifact/document条目，存在且hash一致。阶段原始artifact文件35+69+64共168个已逐文件复制并核对到主目录`artifacts/three-phase-delivery-2026-10-25/previous-phases/`，源及目标无hash差异。

原阶段owner另外只读导出各自SDD的brief/report/review/diff包及闭环原件：暂存安全29、上游观测25、jobs索引19，共73个文件；各自源/复制hash相等。root再核对并复制至D主目录的`artifacts/three-phase-delivery-2026-10-25/review-packets/`，加3份owner清单共76个文件。旧SDD未改，未把公开摘要冒充原始review包。

归档清单分别为本轮artifact根下`previous-phases-archive-manifest.json`和`review-packets-archive-manifest.json`，D各子目录也保留对应清单。本轮完整测试及烟测已结束，最终本轮日志/远端核对/原件清单会在文档同步及工作记录结束后归档并写最终manifest，避免把仍在写入的日志当作固定证据。这些文件均被Git忽略，两个远端只包含源码和文档；跨机器交接需另外携带artifact包，私有环境和原工作树仍在本机保留。

## 运行边界

15:27原服务PID48512、启动时间`2026-10-23T14:09:17.6870660+08:00`、uvicorn无reload标志、ready200；合并前未发现有效自定义Git hook。15:49:31双推后只读核对同PID/start保持、ready200，SQLite/Qdrant local OK、可选Redis degraded；D.env SHA256仍为`6128C4BD1F1BC0E43AB5189BAD25FA160CB8567B555B1B0CC5921E8771B0E1BD`，未读取内容。未restart/reload或对业务存储执行整改写入。

主目录Git源码更新与运行切换分别记录，既有进程探针不能证明新版本已部署。正式切换写入路径仍须沿用既有交接的备份、停写、排空旧写者和统一升级要求；本事项不执行切换或真实数据恢复。

## 主代理裁决及代价

1. 交付前补一次完整后端集成；代价是增加一轮低优先级2worker回归，前端/锁未变不重复构建。
2. 按用户已选定的合并双推和阶段顺序执行；代价是共享源码会更新，部署仍保留独立边界，不强推。
3. 保留原工作树、环境、SDD及未跟踪用户文件；代价是占用本机磁盘。
4. 交付源码与运行切换分开核对；代价是本轮发布源码后仍需独立安排运行验收，不把旧进程就绪当部署成功。

## 继续顺序

完成第一阶段双推及唯一工作记录后，开始KB清理目标快照复用，再优化目录查找；各专项先写精确计划，由Luna并行实施/独立复审、root统一验收。随后处理403/Compose契约、历史暂存恢复写流程，再推进容量/SLO及长期数据架构；不在第一阶段完成时结束整个用户请求。
