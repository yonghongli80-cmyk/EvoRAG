# 审计提交与隔离恢复合同整改

## 目标与既有授权

依据[复查](../../reviews/2026-10-27-project-review.md)，修复可证实的假失败、错误成功审计、归档部分提交和隔离恢复边界；兑现上一批“命令级故障矩阵”的可本地验证子集。用户已指定先审查再修复、并发Luna、root统筹，本设计只收拢现有同库事务，不新增部署/产品流程或要求用户再次选择执行方式。

## 选择与代价

采用既有命令提交点，将组织成功审计与本次SQL状态一起提交。忽略审计异常会丢审计；业务提交后重试写审计会保留假失败/重复；全面outbox/UoW将扩大schema和恢复协议，本轮没有必要。代价是审计不可用时本次SQL mutation失败闭合，需要调用方重试；文件/向量等外部效果仍按既有恢复协议处理，不能假称跨存储原子。

归档复制、检查和hash放在SQL事务外；容器/版本/manifest/entries与组织审计在一个短写事务内，参与helper不得直接commit。普通文件晋升保留既有reservation/移动/补偿协议，只把审计并入finalize回调。KB删除在现有终态transaction内先审计再cleanup，以免policy删除后漏审计。

## 行为合同

1. 组织KB普通上传：审计插入失败不留下新发布版本/job，既有当前版本/job/owner不变；stage按现有晋升补偿保留。成功一次只记一次document_uploaded。
2. 归档初次上传：任一entry/审计/最终commit失败，容器/版本/manifest/entry写入一起回滚；旧容器owner/历史版本保持，quarantine仅清本次创建文件，调用方源文件不删。last_*仅表示成功提交。
3. 归档确认、回收、恢复：组织审计在既有SQL提交点内，失败回滚相关SQL；归档确认继续执行已有文件补偿。确认相同idempotency key重放不新增审计。
4. 整理确认/回滚：只有本次真正applied/rolled_back的项产生成功审计；同action同KB本次命令去重。conflict、原本未应用、already_rolled_back及confirm replay不产生额外成功事件。审计失败使本命令SQL回滚。保持现有响应结构与metadata/CAS/ACL；不借此实现新的全局幂等键协议。
5. KB删除：审计与终态SQL共同提交或回滚；外部清理后失败仍保持deleting并允许重试，恢复成功后只有一条已提交kb_deleted。不能断言被删文件已回滚。
6. restore-test：报告在发布目录前准备好；报告失败只清本次拥有的临时目录，正式目标不出现，随后可重试。既有目标不覆盖。
7. 配置到项目外的本地Qdrant目录同样受restore-test保护，包括与目标的祖先/后代重叠。保护信息从构造context时的设置传递；不读取目标存储，不启动Qdrant。
8. SQL helper继续允许SQLite合法非canonical/quoted名称，值保持绑定、NUL拒绝。明确自定义SQL、旧catalog、dataset与snapshot信任来源，不收紧历史schema兼容。
9. 显式runtime SELECT工具对文件数据库按只读模式打开；缺失文件失败而不创建空库，合法含空格/Unicode/#/%的路径仍能查询，原SELECT限制/query_only/1000行上限及:memory:兼容保持。此项先独立行为RED，不把调用方传入的db_path伪称HTTP任意文件漏洞。

## 实施边界

不改业务schema、HTTP路径/成功响应格式、授权优先级、原服务磁盘源码或私密数据。所有故障测试为临时SQLite、合成文件和fake投影，不进行模型/HTTP外呼或正式压测。新helper若抽取，仅承接原有org policy及action/target/result/reason合同；不要让无组织scope的旧调用新增成功事件。

精确故障语义必须由真实SQLite事务、触发器/延迟约束或受控提交故障验证。嵌套helper写连接的直接commit、reader看不到未提交写入、锁顺序倒置与后置异常，均是复审重点。
