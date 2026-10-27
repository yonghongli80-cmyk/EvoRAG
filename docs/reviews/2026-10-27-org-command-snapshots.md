# 2026-10-27 组织管理提交与授权快照

## 范围与状态

用户要求先合并双推上一批，再继续并发6.1 Terra推进本地待办。上一批已快进交付两端main至ce6da1b，标签一致，无强推；本批从该提交在C隔离分支codex/org-command-snapshots-2026-10-27继续。原D目录main保持99834ca，服务器验收保留，旧补充包保持8eac1fa，不因本批未验改动更新。

本批最终受测冻结为b7e8867bf84114254bcf8e5920b5bc0d5b241ffd，**完整后端3591通过/5跳过、2171.08秒/native0**，JUnit3596项0失败0错误；628受测文件前后完全匹配。产品实现与6cfd052字节一致。首轮3590通过/1个测试代理接口失败/5跳过及修复历史保留在下文，不用上一批3572/5替代本批证据。见[设计](../superpowers/specs/2026-10-27-org-command-snapshots-design.md)、[计划](../superpowers/plans/2026-10-27-org-command-snapshots.md)和[作用域所有权图](../architecture/organization-command-ownership.md)。

## 已实现

- assign_knowledge_base_policy、update_person、create_grant、revoke_grant的成功audit加入原业务transaction；与policy/person/grant/security_version共同提交或回滚，保留原HTTP/admin/no-op和安全版本策略，不添加全局请求幂等。
- OrgAuthorizationService的六个公开方法在单次调用及其嵌套决策、集合和metadata读取中共享同一thread-local只读SQLite快照。创建的reader事务在退出时释放，已有外层reader事务只借用；不获取writer锁、不存共享调用态连接、不更改默认repository独立读合同。
- 四写六读所有权图绑定10入口、34个helper/test定义行和原始SHA，root纯AST/文件验证44引用通过；scope为scoped_only，writer_completeness仍not_proven。

授权快照固定于第一次SQL读取，允许稳定旧视图或新视图。它不保证响应发出时仍是最新权限，也不把鉴权和后续业务写入变为共同writer snapshot。完整writer admission、跨进程fencing、UoW/PG与多实例仍未完成。

## 实际分项证据

既有组织路径基线21通过/122.80秒/native0。新增RED为16失败/3通过、29.03秒/native1：四命令8个语句/deferred commit切点确认业务已提交而审计失败；8个读取反例确认敏感scope/qualification拼接允许、metadata/role与document id视图不一致及跨线程旧视图错误。

修复后联合48通过/114.25秒/native0，覆盖新19项和既有组织admin/audit/identity/kb guard/policy/acceptance/retrieval及授权边界。其后只修service导入排序，三个新文件格式调整均有AST等价记录；全后端Ruff通过。两个实现任务与所有权图均经独立SPEC/QUALITY通过，整批静态复审未发现本批P1/P2/P3可操作问题；这些不替代最终冻结版本的完整运行验收。

原始日志、JUnit、source map校验及过程脚本保留在artifacts/org-command-snapshots-2026-10-27；本计划独立SDD目录保留源码快照和审查报告。过程脚本包含本机环境路径，不作为新机直接运行入口。

## 冻结与运行锁

本地代码提交1643e01（管理命令audit）、4d2703c（授权读取快照），随后6cfd052保存所有权图及计划/阶段报告。root在提交后再次核对10入口+34helper/test定义及rawSHA，44引用仍匹配；source map保留其“提交前工作树绑定”来源描述，不伪造为基线ce6da1b的文件hash。

生产依赖锁烟测5项通过、2.781秒/native0、SQLite3.49.1：四个管理命令实际audit触发器失败回滚及合法重试，以及敏感库两个各自拒绝状态不拼成允许。12次SQLite打开均在私有路径护栏中；执行的是内部函数和读取服务，不冒称HTTP鉴权、服务部署、真实业务或性能验收。生产127项、开发133项已安装版本与锁重新核对零偏差，无依赖安装/升级。

冻结后SQL清单212文件（208Python/4SQL）、859静态SQL、103动态项、0错误；scanner依覆盖不完整返回1。writer234文件1699语法候选、0错误、parse_complete=true/native0，writer_completeness仍not_proven；数量降低来自语法/上下文重分类，不能解释为运行时writer减少。

102条身份与全部来源/evidence hash未变的记录保留，1条变更来源经独立Terra复核后合并。最终103条来源处置完整，90understood_shape/10needs_followup/3external_source，418引用绑定32个Rag源文件；API/CLI均退出0，portability_readiness仍not_assessed，human_reviewed=false。首次复核JSON遗漏finding自身sink引用，严格合并器拒绝后已补足并真实重验，失败记录保留；没有静默接受不完整证据。

## 完整回归首次失败与测试代理修正

首轮完整后端于15:19:47至15:47:36运行，实际退出1；3590通过/1失败/5跳过、1662.52秒。唯一失败test_material_visibility_batch_plan::test_mixed_org_and_legacy_plan_omits_denied_org_rows的ConnectionCounter只转发execute/getlimit，新读取快照访问in_transaction时报AttributeError。官方源核对628文件均匹配，排除运行期间源漂移。

仅给该局部测试代理补充in_transaction属性和rollback直接委托，共7行；没有修改生产代码、ACL/行集合/查询数断言或预期值。相关材料可见性批计划、授权快照和权限缓存隔离27通过/27.57秒/native0；定向独立复审确认修正与失败相符、无新增可操作风险。首轮日志/JUnit和推断/修复报告均保留，最终完整重跑另用新标签，绝不覆盖失败证据。

测试树重新冻结后，生产锁原5场景再次通过，3.25秒/native0，仍为12次私有SQLite打开；所有权图44引用也再次匹配。Rag与scripts/config/依赖锁对6cfd052零差异，SQL/writer清单重新扫描保持859/103与234文件1699候选。103条SQL记录全部满足身份及源/evidence哈希完全一致的保留条件，无新增来源需复审；最终API/CLI校验退出0，418引用/32Rag文件，PG仍not_assessed。它是前述102保留+1独立审查后的测试树重封来源继承，不是跳过变更源复核。

## 最终完整验收与交付边界

修正计数代理后的完整重跑于15:56:32开始、16:32:51原生退出0：3591通过/5跳过、2171.08秒，父进程2179.062秒，JUnit3596项0失败0错误。5跳过保留Windows fork/FIFO/terminate限制三项与缺真实脱敏表格fixture两项，没有删减原权限或查询数断言。

官方source-check在HEAD仍为b7e8867时再次核对628文件，退出0，manifest摘要beb6e10c2b45dd10d9aaf20368f923885329d916364af5f12513f9bcbbefff73。16:33只读环境核对：原Dmain仍99834ca、跟踪树干净、配置哈希与开始一致、8080开始和结束均未监听，C无.env；未启动/停止原服务、未访问业务库。verification-summary.json交叉检查两次完整运行、最终JUnit/源清单、生产锁、SQL源绑定、所有权图及环境记录。

本批三任务和最后测试适配均已独立审查通过，无未关闭的重要问题。新成果保留在codex/org-command-snapshots-2026-10-27，本批没有再次合并双推；已交付main仍以14:42核对的ce6da1b为交付点。按用户要求服务器验收继续留空，旧代码/测试补充包暂不更新。

## 保留的待办

服务器资源门禁/正式矩阵/P99/SLO、真实业务迁移恢复、真实脱敏fixture/供应商证据、Linux专项继续未验。其他业务命令writer图和故障矩阵、授权writer admission、10项SQL输入follow-up、未知commit结果、PG/UoW、共享状态/outbox/多实例按既有依赖继续，不由本批局部修复冒称完成。
