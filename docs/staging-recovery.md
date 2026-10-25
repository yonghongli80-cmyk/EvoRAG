# 暂存恢复维护手册

此流程只处理两类经过内容证明的历史断点：staging版本声明路径缺失、且操作者显式提供同document/version目录内的候选文件；以及非`rag_docs`知识库的未过期`pending_confirmation`归档，其旧global stage文件可按原字节copy到该KB的hashed staging。所有判断以当前数据库身份、ACL、可信版本SHA256和文件安全检查为准。

恢复接口是维护用Python API，提供`preview_staging_recovery`和`apply_staging_recovery`。固定的`audit_staging` CLI仍为只读；本流程没有可写CLI或HTTP路由，也不会自动发现候选、重建索引、确认归档、激活版本、安排重试任务或删除源文件。

## 维护前

1. 取得独立授权并记录操作人、理由、预期映射和变更窗口。维护确认参数是可信调用者的前提声明，不是锁或授权凭据。
2. 停止所有会写入相关数据库或版本目录的业务进程、后台worker和外部文件写者。Python接口的路径检查不能抵御有权限的外部进程并发替换文件。
3. 做一致性备份，包含SQLite数据库及其正常备份所需状态，也要单独保留完整版本目录，特别是`staging`和`archive_work`。验证备份可读且与文件副本对应；不可为取得快照直接删除live WAL/SHM。
4. 根据数据库内document/version/manifest身份与内容hash，人工列出唯一显式映射。先检查候选字节的来源和SHA256；文件名本身不是身份凭据。不要对真实数据拼接未经审核的命令或输入。

## Preview 与 apply

1. 使用`RecoveryRequest`逐项提供操作key、恢复种类、actor、document、version。版本指针修复须显式提供同document/version目录内候选路径；archive rehome只需manifest ID，源路径从数据库manifest读取。不得让接口扫描目录并代替操作者挑选。
2. 执行preview并复核返回的source、destination、身份、hash、字节数和metadata指纹。Preview不创建目录或文件，不写业务metadata，不写回执，也不做迁移DDL。任何ACL、owner、job、引用、路径、状态或字节检查失败都应停止并先调查。
3. 经独立复核后，调用apply并显式声明维护已确认。Apply会重新加载操作者与当前ACL，在同步KB mutation permit及顶层`BEGIN IMMEDIATE`事务内重算计划、校验全局引用、hash与CAS；不可复用旧计划绕过复核。
4. 核验返回的持久回执：operation key、actor、document/version/manifest、源/目标路径、hash、大小及`source_preserved=true`须与已复核计划相符。相同请求重放应返回同一回执；同key异请求必须作为冲突处理。

## 失败处理与范围

失败时保留源、候选、已复制字节和原身份，不手工删除“孤儿”copy。若提交失败或进程中断，只有目标copy完整且重新验证为同hash时，才可在确认数据库状态和ACL后用同一请求key与参数重试；部分或不同hash目标必须保留并拒绝覆盖。重新调查并独立复核后，如需另起操作，使用新的operation key。没有成功回执不代表metadata已提交。

版本指针修复只更新`document_versions.file_path`；归档rehome只更新manifest源路径，必要时同步更新仍指向旧源的staging version路径。原始字节永久保留，归档状态、有效期及entries不变。恢复不等于重建索引，索引重建属于另行授权和计划的维护工作。

本轮验收仅针对合成数据库和临时目录；本文不包含可直接代入真实路径、数据库或凭据执行的恢复命令。遇到current/historical版本、rag_docs归档、过期/已确认manifest、删除态、在途job、共享引用、身份不匹配或无法证明安全的路径时，流程拒绝恢复并保留现场。
