# 历史暂存受控恢复（2026-10-25）

状态：三个任务实现、独立分项及两项修复波次的增量复审通过，整专项复查和root统一验收待完成。基线380813f，分支codex/staging-controlled-recovery-2026-10-25；D main仍1d413ed，本专项未合并、双推或部署，未操作业务文件或数据库。

## 行为和范围

[设计](../superpowers/specs/2026-10-25-staging-controlled-recovery-design.md)、[计划](../superpowers/plans/2026-10-25-staging-controlled-recovery.md)、[维护手册](../staging-recovery.md)为有效入口。只读audit CLI保持inspect_only，没有新HTTP或可写CLI。

- version_pointer只处理staging版本的root内缺失声明，候选由调用者显式指定，必须位于同document/version目录并与DB可信SHA256相同；只修路径，不激活或索引。
- archive_rehome只处理非rag_docs、未过期pending_confirmation的staging归档；核对container/version身份，复制旧global stage到KB hashed stage，CAS更新manifest及必要的version路径，保留原文件/归档状态/entries。
- apply要求可信维护调用者明确声明停写，重新加载actor与既有ACL、同一同步KB permit、顶层BEGIN IMMEDIATE、全局路径/身份引用和文件hash重查。SQLite业务路径与v6持久回执同事务；不假装文件系统与数据库共享原子事务。
- 重放在当前ACL之后检查CAS后身份、归档状态/有效期、引用和内容。内部post-state hash只存回执JSON，公开字段保持。缺证明或状态变化拒绝，不能把旧回执当成当前授权。

## 实施与分项证据

文件模块d3614aa，复审提出ctime及fstat异常句柄关闭缺口，272c4c6修复并增量复查ADDRESSED。真实Windows观察到同一文件lstat/fstat ctime约1ms差异，最终只在同来源前后比较ctime，跨来源比较共同身份字段。原12通过/1 FIFO跳过，修复后定向5通过；完整文件最终统计等待根验收。symlink、真实junction及hardlink场景已执行。外部文件写者仍必须排除。

回执模块bbba584，真实UserDB v6新建/模拟v5升级、重复迁移、CAS与deferred commit失败回滚，连同既有迁移/启动文件9通过。初轮手写小schema夹具缺表/字段错误保留日志，修正为真实app升级路径，未放松migration runner。独立规格/质量PASS；一个非阻塞P3（所有IntegrityError被误报为key冲突）交整专项复查裁决。

协调器7bab668初次19通过；实现前未跑RED，是已记录的过程偏差，不冒称完成TDD。初轮长Windows路径、测试变量、ctime和localized message断言问题均保留原日志。独立复审发现成功回执重放的identity/ref检查弱于首次apply：真实新增9用例在修复前全部失败，4a7f402/ff6e473修复后31通过，增量复查PASS。原件、copy、旧metadata在失败时保留；公开回执重放完全相同。

证据在artifacts/staging-controlled-recovery-2026-10-25，所有数据为合成Temp，offline/BelowNormal/BLAS1。重叠的定向测试不相加为统一通过数。统一JUnit、生产锁实际API烟测、Ruff及原服务核对待补。

## Root裁决及代价

1. 仅两类有严格证明的staging恢复；代价是current/historical、confirmed/expired、rag_docs archive和歧义案例继续拒绝，需另行调查。
2. 仅可信Python维护接口，维护确认不是安全凭证；代价是操作者仍须独立授权、备份和排除外部写者，不能直接开放网络。
3. 源与失败copy均保留；代价是存储增长。完整同hash copy可同key重试，partial/mismatch copy保留且拒绝覆盖，另起key需重新调查复核。
4. v6 app迁移与transaction-only repo；代价是以后部署才会安装新表，不能将本地合成验证称运行切换。
5. preview在顶层事务复用既有ACL，拒绝时连同嵌套OrgAudit写回滚；代价是短时占用writer wrapper，不新增个人/组织角色门禁。
6. Windows按同来源比较ctime；代价是不能承诺抵御全部文件系统竞争，外部停写前提保留。
7. 回执持久化内部post-CAS状态证明、公开字段保持；代价是后续metadata/ACL/引用变更使旧回执失效，无证明旧回执fail closed，需调查而非强行重放。

## 接续

本专项只处理受控恢复代码，不执行真实恢复或部署。timer T-A9195D72ED29待最终根验收和实际submit；之后继续总计划容量/SLO与长期架构。用户未知业务规模，分档文档数/并发仅为工程实验假设，不构造正式业务SLO。
