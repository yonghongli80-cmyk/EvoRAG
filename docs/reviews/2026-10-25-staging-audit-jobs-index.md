# 暂存审计 jobs 关联索引优化（2026-10-25）

状态：实现、两项独立复审及各一轮测试修复增量复审通过，整分支复查及root统一验收待完成；未合并或部署。

## 范围与实现

分支`codex/staging-audit-jobs-index-2026-10-25`从`9a8653c1e4fc51d462268aa8c9fb54403a972720`继续，复用C隔离工作树。此前本地暂存安全和上游观测修复保留在分支链中，D主目录与运行实例未切换。[设计](../superpowers/specs/2026-10-25-staging-audit-jobs-index-design.md)、[计划](../superpowers/plans/2026-10-25-staging-audit-jobs-index.md)及[操作指南](../staging-audit.md)为本轮入口。

`_collect_database_references`在原完整jobs行校验之后，将满足原material scope/type过滤条件的行引用按version存入有序桶；保留SQL、原jobs列表和行对象。`_diagnose_version`查对应桶，原active状态判断、statuses、pending job_ids及全部报告阶段保持。关联部分从Θ(V·J)变为O(J+V+匹配行)，额外引用/桶空间O(J)；不据此推断整个审计线性。

## 分项证据

- Task1：产品提交`fd3ed47`仅为6行新增/8行删除及计数测试。旧版V=1/12/48、J=3V+1的全局jobs遍历为2/13/49次，行访问8/481/7105；索引后为2/2/2次、8/74/290，jobs只装载一次。纯模块3项RED/GREEN日志保留，不能当作正常conftest已通过。
- Task2：`5001882`新增四类完整报告golden、七类完整unsupported_schema错误守卫；期望来自冻结基线模块SHA256`71b30b8c7a1322e57f483941b625acb0e967ec3cb157ac6a720b3409b4353b98`。只规范临时绝对路径前缀，不删除字段或重排列表；永久测试不依赖Git或ignored artifacts。旧版语义检查及当前正常conftest定向11项通过，输入DB/目录hash保持。这些是等价守卫，不伪造初始RED。
- Task3：基准脚本已准备并compile检查，冻结后执行。同一显式合成快照按N=100/500/1000、每种实现3轮old/new交替；每个N实际jobs=3N，包含material、孤儿和非material。完整报告逐值对照，输入hash前后核对，记录耗时/峰值内存；不设壁钟通过阈值。

Task1复审提出测试覆盖P2：原计数用例仅一条active job，未锁住多active pending ID顺序和索引行身份。主代理核实后交原实现者补独立语义守卫，保留原计数用例及真实RED历史；测试修复`186d203`已验证多active pending ID顺序、原jobs列表与桶内行身份，正常conftest4项通过；增量复审ADDRESSED且无新问题。Task2的同类golden缺口经`c7b4cd3`补一条retry行，以冻结基线重新生成对应报告并明确pending IDs为`[15,55]`，其它三份有效报告及七份错误报告保持；正常conftest11项通过，增量复审ADDRESSED且无新问题。最初review包因PowerShell嵌套数组被写成`System.Object[]`，原错误包保留，主代理已用扁平字符串列表修正，不以无效diff作为验收证据。

## 统一验收

root冻结后的审计/恢复集成/架构组合、Ruff、生产锁CLI烟测、基准结果及最终服务只读核对待补。本轮不重复与纯审计模块无调用关系的完整后端；前一轮3248/4是基线，不计入本轮数字。

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

代码仍在隔离分支，未合并/双推/部署；公司唯一记录`T-DE8458BE97A6`待最终验收后实际submit。下一项是独立规划KB清理目标快照/目录查找优化；403/Compose策略、历史暂存恢复写流程、业务容量/SLO及长期存储/多副本架构继续保留，不混成本轮完成项。
