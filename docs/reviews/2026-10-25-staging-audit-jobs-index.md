# 暂存审计 jobs 关联索引优化（2026-10-25）

状态：实现、两项独立复审、各一轮测试修复增量复审、整分支复查及root统一验收通过；未合并、推送或部署。统一受测源码/测试冻结为`e52237c3da18457ca508ac407dc13e2c71131920`，产品代码为`fd3ed47`；后续仅补交接和记录回执。

## 范围与实现

分支`codex/staging-audit-jobs-index-2026-10-25`从`9a8653c1e4fc51d462268aa8c9fb54403a972720`继续，复用C隔离工作树。此前本地暂存安全和上游观测修复保留在分支链中，D主目录与运行实例未切换。[设计](../superpowers/specs/2026-10-25-staging-audit-jobs-index-design.md)、[计划](../superpowers/plans/2026-10-25-staging-audit-jobs-index.md)及[操作指南](../staging-audit.md)为本轮入口。

`_collect_database_references`在原完整jobs行校验之后，将满足原material scope/type过滤条件的行引用按version存入有序桶；保留SQL、原jobs列表和行对象。`_diagnose_version`查对应桶，原active状态判断、statuses、pending job_ids及全部报告阶段保持。关联部分从Θ(V·J)变为O(J+V+匹配行)，额外引用/桶空间O(J)；不据此推断整个审计线性。

## 分项证据

- Task1：产品提交`fd3ed47`仅为6行新增/8行删除及计数测试。旧版V=1/12/48、J=3V+1的全局jobs遍历为2/13/49次，行访问8/481/7105；索引后为2/2/2次、8/74/290，jobs只装载一次。纯模块3项RED/GREEN日志保留，不能当作正常conftest已通过。
- Task2：`5001882`新增四类完整报告golden、七类完整unsupported_schema错误守卫；期望来自冻结基线模块SHA256`71b30b8c7a1322e57f483941b625acb0e967ec3cb157ac6a720b3409b4353b98`。只规范临时绝对路径前缀，不删除字段或重排列表；永久测试不依赖Git或ignored artifacts。旧版语义检查及当前正常conftest定向11项通过，输入DB/目录hash保持。这些是等价守卫，不伪造初始RED。
- Task3：基准脚本经root检查后，在第一次执行前将声明路径改为document/version owner形状的缺失文件；版本根目录为空，没有材料正文。同一显式合成快照按N=100/500/1000、每种实现3轮old/new交替，每个N实际jobs=3N，包含material、孤儿和非material。冻结后18次调用退出0，完整报告逐值对照、重复报告稳定且输入hash前后保持；记录耗时/峰值内存，不设壁钟通过阈值。

Task1复审提出测试覆盖P2：原计数用例仅一条active job，未锁住多active pending ID顺序和索引行身份。主代理核实后交原实现者补独立语义守卫，保留原计数用例及真实RED历史；测试修复`186d203`已验证多active pending ID顺序、原jobs列表与桶内行身份，正常conftest4项通过；增量复审ADDRESSED且无新问题。Task2的同类golden缺口经`c7b4cd3`补一条retry行，以冻结基线重新生成对应报告并明确pending IDs为`[15,55]`，其它三份有效报告及七份错误报告保持；正常conftest11项通过，增量复审ADDRESSED且无新问题。最初review包因PowerShell嵌套数组被写成`System.Object[]`，原错误包保留，主代理已用扁平字符串列表修正，不以无效diff作为验收证据。

## 统一验收

- 冻结`e52237c`后，原生正常conftest、2 worker/loadfile、BelowNormal、BLAS1、offline guard及全新短basetemp统一执行五个文件：**45通过、0跳过**，pytest计时26.34秒、退出0；JUnit45项，0失败、0错误，suite计时26.262秒。组成是原审计23、恢复集成5、索引4、兼容11、架构2，不用分项结果拼数字。全树Ruff及两份新测试format通过。
- 生产锁Python3.12.10的实际CLI烟测4项通过、脚本退出0：最小jobs列/顺序成功，apply拒绝、WAL sidecar拒绝和非法jobs metadata拒绝分别返回预期CLI退出码`0/2/2/2`。子进程在加载CLI之前启用offline/BelowNormal及禁止应用导入的guard；快照和目录hash保持。
- 15:13:20（Asia/Shanghai）只读核对原PID48512及启动时间`2026-10-23T14:09:17.6870660+08:00`保持，`/ready`200、SQLite/Qdrant local OK、可选Redis degraded。D `.env` SHA256仍为`6128C4BD1F1BC0E43AB5189BAD25FA160CB8567B555B1B0CC5921E8771B0E1BD`，隔离树无`.env`；D main仍为`3f52490`，跟踪文件干净。未重启/部署或调用模型，测试未连接业务存储。
- root汇总核对产品/测试/锁文件相对冻结提交无漂移，烟测、Ruff和基准来源hash一致；整分支Luna复查APPROVE、无代码或测试发现，其剩余运行验证项已由上述证据关闭。本轮不重复与纯审计模块无调用关系的完整后端，前一轮3248/4仅是基线。

## 旧新合成测量

每规模3轮的中位数如下。耗时包含tracemalloc、SQLite读取、路径检查及报告构建；测量时未并发运行pytest。每规模使用一份相同快照，报告分别为N条版本entry及2N条findings，全部旧新结果逐值一致。

| 版本 / jobs | 旧耗时(s) | 新耗时(s) | 耗时减少 | 旧/新tracemalloc峰值中位数(bytes) |
| --- | --- | --- | --- | --- |
| 100 / 300 | 0.589 | 0.453 | 23.2% | 521701 / 537589 |
| 500 / 1500 | 3.136 | 2.793 | 10.9% | 3051550 / 2761014 |
| 1000 / 3000 | 6.787 | 5.885 | 13.3% | 5735477 / 5982829 |

这些是本机低优先级合成诊断，样本每种实现每规模只有3次，不能作为业务容量/SLO。新增索引仍需O(J)额外引用空间；单次分配峰值会受分配器/GC及其它构建开销影响，500规模的下降不意味着索引保证省内存。与先前审计的输入jobs数量、manifest数据及测量条件不同，比较限于本轮相同快照的旧新结果。

原始结果为`benchmark-results-attempt-1.json`，SHA256`40a175d840917f3c539b5a21ecc953424d84a2eff55a6e3f1dc55fc5cdee5477`；`verification/benchmark-summary.json`保留全部原始来源信息及中位数。统一JUnit为`verification/audit-gate-attempt-1.junit.xml`，SHA256`2856c6370bc470358baa99bb2a89d044ae0cb6d5b1a2646cd3510c4444cf17c4`；`verification/verification-summary.json`记录实际退出码、分文件计数、源码冻结等价和日志hash。

首次Task1正常conftest执行在断言之前失败：启动命令把TMP/TEMP/TMPDIR及`--basetemp`指向同一目录，collection创建的session users.db随后落入pytest首次清理范围，Windows返回文件占用错误。原工具调用参数已由实现者确认，属于测试启动目录冲突；没有修改应用或全局fixture来绕过。root统一gate保持正常conftest，并保留默认系统Temp及独立、未预建的basetemp。所有涉及文件均为合成Temp输入，不涉及业务库。

证据根目录为Git忽略的`artifacts/staging-audit-jobs-index-2026-10-25/`；SDD、工作树和环境保留。跨机器交接须另外携带原始证据。

## 主代理裁决及代价

1. 原完整校验后建立内存material索引；代价是增加O(J)引用/桶，路径和报告输出成本仍在。
2. 按用户授权自主完成设计、先计划再并行，不增加审批停顿；代价是设计偏差需本地返工，未授权自动合并/推送/部署。
3. 三路Luna使用互不重叠的产品/测试/fixture/基准文件；代价是原子patch/compile及统一冻结需要root协调，root统一Git与验收。
4. SDD辅助操作用原生PowerShell/Python，避开已有MSYS故障；代价是root核对目录归属、brief和review包内容，并修正已发现的数组展开问题。
5. 本轮验收覆盖全部直接审计消费者、恢复集成、架构和CLI/对照测量；代价是无关后端未重跑，旧全套基线不能当作本轮新增结果。
6. 保留SDD、日志和环境；代价是占用本地磁盘，后续清理由明确请求决定。

## 交付与接续

代码仍在隔离分支，未合并/双推/部署；公司唯一记录`T-DE8458BE97A6`待文档提交后实际submit，回执待补。下一项是独立规划KB清理目标快照/目录查找优化；403/Compose策略、历史暂存恢复写流程、业务容量/SLO及长期存储/多副本架构继续保留，不混成本轮完成项。
