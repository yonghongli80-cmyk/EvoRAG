# 组织知识库权限运维手册

## 权限边界

- 固定账号为 `demo001` 至 `demo280`，初始密码统一为 `123456`，不强制首次修改。
- 知识库属于公司或组织，不属于个人。库内文档继承知识库权限，不能单独设为私密、共享或公开。
- 普通成员读取其组织及上级组织知识库；负责人管理明确管辖节点及其后代；公司管理员管理全部范围。
- 敏感知识库同时要求组织范围和对应资格。显式拒绝优先于管理员以外的其他规则。
- 文件页面的个人私密、共享、公开权限保持不变。

## 首次初始化

在仓库根目录执行。先创建并验证备份，再初始化；初始化可安全重复执行。

```powershell
$backup = (.venv\Scripts\python.exe scripts\backup.py create --trigger manual | ConvertFrom-Json)
.venv\Scripts\python.exe scripts\backup.py verify $backup.backup_id
.venv\Scripts\python.exe -m rag.org_auth.seed --db-path data\users.db
.venv\Scripts\python.exe -m rag.org_auth.seed --db-path data\users.db
```

第二次执行应报告 `people=0`、`units=0`、`knowledge_bases=0`。初始化会保留管理员后续修改的显示姓名、职位等字段，并把此前没有组织归属的旧知识库标记为 `unassigned`；此类知识库仅公司管理员可见，待管理员重新分配归属。文件页面权限记录不会被迁移。

默认初始化会生成 Markdown 资料并将其记录为待索引。已配置可用嵌入模型时，可恢复式执行内容索引：

```powershell
.venv\Scripts\python.exe -m rag.org_auth.seed --db-path data\users.db --index-content
```

仅需要账号、组织和知识库元数据时使用 `--metadata-only`。

## 验证与启动

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check rag tests --select E9,F63,F7,F82
Set-Location frontend
npm test
npm run build
Set-Location ..
.venv\Scripts\python.exe start.py
```

浏览器访问 `http://localhost:8080`。分别使用普通员工、负责人和公司管理员账号登录，确认知识库列表不同；再确认文件页面仍可使用私密、共享和公开权限。不要在登录页展示初始密码。

## 日常管理

公司管理员可在“组织权限”页面修改职位、主部门、兼职部门、负责人范围、敏感资格和账号启用状态。停用账号或改变安全版本后，旧令牌立即失效。权限预览只计算实际可见范围，不会签发模拟登录令牌。

拒绝访问、人员变更和特别授权会写入组织审计表。审计不记录密码、令牌、查询正文或文档正文。

## 回滚

1. 停止应用，避免恢复期间产生写入。
2. 查看已验证备份：`.venv\Scripts\python.exe scripts\backup.py list`。
3. 先做隔离恢复演练：`.venv\Scripts\python.exe scripts\backup.py restore-test <backup_id> <独立空目录>`。
4. 确认演练成功后，按 [部署与备份说明](deployment.md#数据与备份) 的正式恢复流程替换运行数据。
5. 恢复与当前版本匹配的代码并重新启动。

备份清单会注明是否需要重建向量索引。不要在应用运行时直接复制 SQLite 文件，也不要手工删除组织权限表。
