# 组织命令提交与授权读取快照（2026-10-27）

## 目标与当前事实
上一批已快进双推至ce6da1b（受测产品f047a73，3572通过/5跳过）。用户要求继续并发6.1 Terra推进本地待办，服务器验收保留，代码/测试补充包暂不更新。本批从ce6da1b在既有C隔离worktree的新分支codex/org-command-snapshots-2026-10-27继续。

源码显示四个组织管理入口assign_knowledge_base_policy、update_person、create_grant、revoke_grant先提交业务SQL，再独立写成功audit；audit异常会留下已提交状态和失败响应。OrgAuthorizationService则在一个授权决定及集合/展示调用中混用多个独立autocommit SELECT，可拼出未在任何时刻成立的权限组合。

## 方案
root选择先修复上述同库边界并产出对应命令所有权图；不现在引入完整UoW/PG或全局writer admission。单独改文档无法关闭可复现缺口；把所有读取改为writer连接会扩大锁争用和事务语义，不适合此批。

### 管理命令
四入口的成功audit与policy/person/grant/security_version变更参与原有同一个SQL transaction，发生语句或真实commit失败时共同回滚。保留HTTP/返回字段、admin与404/400合同和原安全版本递增规则；不新增请求幂等协议，不根据空body/no-op擅自改变审计策略。

### 授权读取
保持OrgAuthorizationService六个公开方法签名和返回类型：decision、can_manage_org、authorize、allowed_kb_ids、allowed_document_ids、visible_knowledge_bases。每次公开调用及其嵌套决策/集合/metadata读取使用同一只读SQLite快照，快照在第一次SQL读取时固定。允许稳定的旧视图或新视图，不允许两次提交拼出的授权组合。

默认仍用thread-local query_only reader，不获取业务writer锁，不借用另一个线程全局writer的in_transaction状态。短读transaction由本调用建立时正常/异常均释放；若reader已有外层transaction则借用而不提交/回滚外层。连接绑定只存在于本次调用的新repository视图，不修改共享service.repo或跨线程共享连接。users投影保持原get_user_by_id的id/username/is_admin/password_changed_at及bool语义。

建议私有helper authorization_read_snapshot(db) yield (conn, bound_repo)，OrgRepository新增可选keyword-only read_connection供本次读取使用，默认调用者完全不变。全部repository读方法使用该绑定；本批不改变repository写方法及外部直接读的既有合同。不得创建名为UnitOfWork的空壳或SQLite-only迁移完成标记。

### 所有权图
仅追踪四管理命令及六授权公开读入口：实际连接、transaction/commit owner、nested audit、security_version与可见外部效果。以源码函数和最终hash绑定，区分已验切点和未知。图不是完整writer清单；API审计收拢与读快照不构成“从鉴权到写入始终同一快照”的writer admission。

## 验证
真实私有SQLite和合成账户/组织，不访问业务库。TDD先复现audit失败/commit失败/重试与混合授权视图；用事件或明确回调控制第二连接提交，不使用sleep竞争。验证相同服务实例跨线程无连接泄漏、外层reader事务保留、异常清理及WAL writer不被长期阻塞。先分项，再独立复审，源冻结后完整后端、生产锁、SQL源绑定与原目录前后核对。

## 全局约束
- 仅修改C隔离worktree；不切换D目录、不读取.env正文或业务库、不调用真实模型、不开启真实服务/正式压测。
- Python3.12与现有依赖锁，不新增依赖、不做schema迁移，不改变403/404/admin/legacy KB政策和现有嵌套transaction语义。
- 6.1 Terra/high三路文件范围互斥；实现代理不自行提交、切分支、推送、pytest或创建子代理，root串行调度测试并实际独立复审。
- RED_READY即源码/测试冻结点；后续修改先由root排程，防止正在运行的测试加载不同版本。
- 所有SQLite/文件fixture在新OS临时目录，离线网络护栏、低优先级和BLAS单线程。
- 不把读快照当作完整授权writer准入、跨进程fencing、PG或多实例证明；10项SQL输入follow-up和服务器/真实业务验收继续保留。
- 本批默认仅本地验收；已双推的ce6da1b及旧补充包不受本批未验代码影响。

