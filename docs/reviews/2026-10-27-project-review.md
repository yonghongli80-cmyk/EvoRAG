# 2026-10-27 项目复查与修复入口

用户授权先审查，再修复发现及既有待办，继续并发Luna、root统筹。开始版本为 `8184463`，fetch后Gitee/GitHub main与本地一致；产品源码仍承载前批受测 `cdf039a`。本轮隔离分支 `codex/review-remediation-2026-10-27`，未切换D工作区。以下是修复前审查，最终结果另记，不能把拟议测试当已通过。

## 审查方法与范围

三路Luna分别审查SQL输入/metadata/旧库边界、KB删除/上传/索引/恢复命令边界、启动鉴权/备份/维护CLI/前端契约；root核对实际调用链、共享连接与直接commit、历史待办及现有测试。审查以源码和现有合成测试为依据，不读取业务数据库/材料/.env正文，不使用真实模型或外部服务。这是本轮重点路径审查，不是所有执行路径均无缺陷的证明。

修复前六模块隔离基线 **44 passed、51.94秒、实际退出0**。覆盖test isolation、material upload commit、KB lifecycle API、backup restore/CLI和org audit。原始记录在 `artifacts/review-remediation-2026-10-27/baseline.*`。09:33首次核对8080无监听、ready不可达（测试前已如此），Dmain仍99834ca、跟踪树干净、配置哈希与此前一致；C无.env。不启动原服务，不修改旧迁移包。

## 确认需要修复的路径

| 编号 | 修复前位置 | 触发与影响 | 处理任务 |
|---|---|---|---|
| R1/P2 | `rag/materials/api.py` upload finalize之后的组织审计；archive confirm/recycle/restore的后置审计 | 审计失败可能在版本/job或业务状态已提交后导致HTTP失败；普通重传可新建版本/manifest，其他状态命令可能无法重试补审计 | T3a/T3b |
| R2/P2 | `rag/materials/archives.py:upload_zip` 与 `material_repo` 的manifest/entry直接commit | 归档多步持久化中后续失败会清quarantine却留下已提交记录；直接套外层事务不能阻止helper提前commit | T3a |
| R3/P2 | `rag/routers/knowledge.py:coordinate_kb_deletion` 审计早于finish_delete | 终态提交失败时已提交成功审计，重试可能重复；需将同库审计与终态SQL收拢，仍不承诺回滚已完成的文件/向量清理 | T1 |
| R4/P2 | organization confirm/rollback返回后逐项无条件审计 | conflict/未变更项被记成success；同key replay与已rolled_back项再次写审计。同KB多项产生不可区分的重复KB事件 | T3c |
| R5/P2 | `rag/backups/service.py:restore_test` rename后写报告 | 报告I/O失败留下正式目标，下一次restore-test被target_not_empty拒绝 | T2 |
| R6/P2 | `scripts/backup.py` restore-test保护根 | 外置本地Qdrant目录不在project/data内时未受保护，可能被当隔离恢复目的地 | T2 |
| R7/P2 | `rag/tools.py:create_sql_tool` | 只读查询面使用默认sqlite3.connect；真实RED确认缺失路径SELECT1成功建空库、缺表查询报错后仍留空库。改用文件只读打开，保留已有路径与memory兼容 | T5 |
| R8/P2 | `rag/repositories/schema.py:rebuild_document_versions_status` | 三种合成旧表RED确认固定列复制会完成升级却丢失额外普通/生成列。重建前按完整列集合与hidden标志拒绝未知布局，保持已知schema大小写/列序兼容 | T6 |

R1/R2修复不能仅调整HTTP异常：必须验证业务SQL与审计共同提交/回滚，并保留原文件补偿。R3把审计放在cleanup之后会因policy已删除漏记，应在同一终态事务内先读取policy/写审计再执行cleanup。OrgRepository.locked_read使用独立reader；不得宣称所有词法嵌套读取都共享writer快照。

## 旧待办复核

剩余10条动态SQL记录暂未发现当前请求入口可触发的SQL缺陷：自定义迁移/目录、旧库catalog重放、显式runtime SELECT、生成artifact及手构workbook属于具体调用方信任合同。上一设计刻意保留合法quoted/非canonical对象及额外schema列；本轮不改成canonical白名单。补SQLite信任边界说明，PG可移植性仍未验收；不把10条follow-up伪称10个漏洞或通过修改计数完成迁移。

现有历史暂存恢复已覆盖CAS/receipt事务和commit失败保留原件/副本，未确认删除唯一可恢复源的新缺陷。API启动/鉴权与前端401/403静态审查暂未确认新问题，不重复开发已完成部分。

服务器真实矩阵/P99/SLO、真实脱敏fixture/供应商证据/业务质量、真实存量切换仍按既有安排留空；PG/UoW、共享状态、对象存储、多实例和生产shadow按依赖推进，不由本轮局部SQL事务修复冒称完成。

下一步：[设计](../superpowers/specs/2026-10-27-audit-and-restore-contracts-design.md)、[计划](../superpowers/plans/2026-10-27-review-remediation.md)。

## 分项实现与源码冻结前验证

| 范围 | 独立运行证据 | 实现与复审 |
|---|---|---|
| 隔离恢复 | 原始2项行为RED；backup七组72通过/36.06秒/native0 | 报告随temporary一起发布；context追加默认空保护根并传递本地Qdrant路径；scoped SPEC/QUALITY PASS，f2f16c1 |
| 删除/上传/归档/只读SQL | 扩大组106通过/1项Windows fork跳过/119.19秒/native0；覆盖补强后32通过/23.39秒/native0 | audit/终态提交、archive aggregate transaction、hash写锁外与真实取消审计；只读打开不建空库；T1/T3a/T5分别scoped PASS，36d159c/c06f371/f11986a |
| 归档确认/回收/恢复 | 首轮1失败/65通过定位独立提交的container路径；修正后新state+archive组27通过/29.51秒/native0 | file_path与entry hash/激活/manifest/result/jobs/audit同commit，既有补偿保留；scoped PASS，7509838 |
| 整理与旧schema保护 | 组织/新guard/旧材料组43通过/78.09秒/native0；旧版本/migration/FK专项23通过/25.05秒/native0 | 仅真实改变KB的成功audit、replay去重；固定table_xinfo拒绝未知列，已知大小写/列序正常升级；scoped PASS，b85b5ea/dc26363 |

各行有重叠，不能相加当完整后端。原始测试包括明确的故障RED、fixture父行/探针事务错误及修复过程，全部保留于`artifacts/review-remediation-2026-10-27/`；没有把测试夹具错误统计成产品缺陷。格式调整保留AST相等证明；最终根级结果见下节，不复用昨日3481/5。

## 最终根级验收

冻结受测源码为`5c501f15e6592f789a6841a41f01c81756a5a389`，后续交接回执只改文档。

| 检查 | 实际结果与边界 |
|---|---|
| 完整后端 | **3517 passed / 5 skipped，1988.53秒，native0**；JUnit3522项、0failure/0error。单pytest、BelowNormal、BLAS1、离线网络门禁、新建短OS临时目录 |
| 生产依赖锁 | **15个场景通过，native0，10.313秒，SQLite3.49.1**；8个原启动/取消线程/identifier场景与7个新增隔离/事务/归档/recycle/readonly/legacy/backup场景；真实私有SQLite与合成文件，不连接provider/服务 |
| 源码稳定性 | tracked freeze/check的622文件前后一致，manifest摘要`a5249e5f42ea612b2514497d9d305deaa1d0c0b8a1a5d9ba84cc05d64dbdb7ff`；C无.env |
| 静态检查 | 全源树Ruff0；新增模块/测试与本次完整格式化的13文件format-check0；未声称原有全部大文件已通过全树format |
| 独立复审 | 每任务SPEC/QUALITY、冻结整分支和生成证据一致性审计通过；人工审查字段保持false，Luna审查不伪称用户亲自复审 |
| 新SQL来源 | 211文件（207Python/4SQL）、854静态条目、103unresolved、0错误；scanner按不完整合同退出1。82记录仅因身份/全部hash未变而保留，21条实际源复审；最终90understood/10followup/3external，403引用绑定32Rag文件，validator/CLI native0；PG仍not_assessed |
| writer候选 | 233文件、1703 AST候选、0错误/native0；writer_completeness仍not_proven |
| 原目录状态 | 11:47再次核对：8080仍无监听（开始即如此）、Dmain99834ca/跟踪树干净、.env哈希保持；未启动服务、未迁移真实业务数据 |

五项skip为Windows fork/FIFO/terminate限制三项、缺真实脱敏表格fixture两项。生产锁首次试跑误用了新归档的current version参数，第二次触及旧定位验收护栏默认拒绝全部URI；仅验收脚本改为正确参数、并在同一私有根内受限允许`mode=ro` URI，额外验证越界与写URI拒绝。产品代码、正式定位工具门槛和旧失败原件均保持；最终第3次15场景native0，预期注入异常日志不当作失败。

行数取舍：既有`knowledge.py`原1018行，本轮仅局部增加4行为1022，保留已测结构以免为数值做无关重构；其余本轮修改的Rag源文件不超过1000行（schema976、archives952）。大路由职责拆分可独立排期，不假称本批已完成。

12:12实际核对Gitee/GitHub main从8184463快进到受测5c501f1、标签一致、无强推；之后仅追加交接回执，最终main以实际refs及代码测试补充包的code-info/交付回执为准。本机D磁盘源码保留，源码交付不代表部署。用户确认20261026业务迁移包已经拷走，本轮只做新增代码和验收补充，不重新读取/打包业务库或.env。

本地审查整改工作记录已实际submit：`R-20261027-ACFC835E`，pending_review，timer`T-D8F7098E3462`，native0；交付/补充包作为后续独立事项记录。原始证据及所有失败迭代均保留。服务器/真实迁移、完整writer/授权共享快照、PG/UoW、共享存储/多实例与自动生产shadow仍是后续门槛，不能据本批通过称全项目全部完成。
