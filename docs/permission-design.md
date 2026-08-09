# 权限管理设计

## 1. 当前模型

RAGv3 使用基于归属和可见范围的权限模型。文档的有效范围为
`private / shared / public`，授权主体为 owner、被共享用户和系统管理员。

早期版本曾设计 1–5 级权限。该方案已经废弃：数据库中的遗留字段
`permission_level` 仅为兼容旧库保留，`permission_level` 不参与授权判断，
用户 API 也不返回该字段。系统管理员只由 `is_admin` 表示。

## 2. 文档权限规则

### 2.1 查看

满足下列任一条件即可查看：

- 当前用户是文档 owner；
- 文档 `scope='public'`；
- 文档 `scope='shared'`，且 `document_shares` 中存在当前用户的授权；
- 当前用户 `is_admin=true`。

无权限记录的旧文档对普通用户默认拒绝，避免未归属数据被检索、下载或修改；
系统管理员可以处理这类遗留文档。

### 2.2 编辑和删除

- owner 和系统管理员可以编辑、删除、切换公开状态及管理共享；
- `shared` 文档的被共享用户只有在授权为 `edit` 时才可编辑或删除；
- 普通 `view` 共享不允许修改文档，也不允许再次共享。

### 2.3 范围状态

| scope | 可见用户 | 状态变化 |
|---|---|---|
| `private` | owner、admin | 默认上传状态；最后一个共享被撤销后回到此状态 |
| `shared` | owner、已授权用户、admin | 创建文档共享时自动进入此状态 |
| `public` | 所有人 | owner/admin 通过公开状态接口切换 |

`protected` 文档用于系统内置公共文件，保持公开且不能由普通流程改变范围。

## 3. 知识库权限

知识库同样具有 owner、`private/public`、显式共享和 admin 绕过机制。
知识库共享记录支持 `view` 与 `edit`。查看、修改或删除知识库前，接口应调用
`check_kb_permission()`；文档级操作仍需独立执行文档权限检查。

无元数据记录的旧知识库对普通用户默认拒绝，管理员可进入并完成治理。

## 4. 数据模型

### 4.1 用户

权限判断只使用：

- `users.id`：用户标识；
- `users.is_admin`：是否为系统管理员。

### 4.2 文档权限

`document_permissions` 的有效授权字段为：

- `doc_name + kb_id`：文档在知识库内的唯一标识；
- `owner_id`：上传者；
- `scope`：`private`、`shared` 或 `public`；
- `protected`：是否为受保护公共文件；
- `downloadable`：是否允许下载。

`document_shares` 记录 `doc_id`、`user_id`、授权人和 `view/edit` 权限。
删除文档权限记录时，共享记录通过外键级联清理。

## 5. 统一校验入口

- 单文档查看、编辑或删除：`rag.permissions.check_doc_permission()`；
- 检索结果过滤：`rag.permissions.filter_chunks_by_permission()`；
- 可访问文档集合：`rag.permissions.get_accessible_doc_names()`；
- 知识库查看、编辑或删除：`rag.permissions.check_kb_permission()`。

RAG 检索先扩大候选集，再按权限过滤，最后截取 `top_k`。缓存命中的来源也必须
使用相同的权限模型重新过滤，不能把缓存时的可见性当作当前授权。

## 6. 权限管理 API

需要认证的主要文档权限接口：

- `GET /documents/{doc_id}/permissions`：owner、共享用户、公开访问者或 admin 查看；
- `PUT /documents/{doc_id}/permission`：owner/admin 切换公开与私有；
- `POST /documents/{doc_id}/share`：owner/admin 共享给指定用户；
- `DELETE /documents/{doc_id}/share/{user_id}`：owner/admin 撤销共享；
- `PUT /users/{uid}/role`：admin 设置目标用户的 `is_admin`。

上传、下载、标签、删除和知识库文档接口也必须在各自的资源边界执行权限校验。

## 7. 安全边界

- 授权判断必须同时包含 `kb_id` 与 `doc_name`，防止跨知识库同名文档串权；
- `scope` 是唯一可见范围来源，遗留的 `is_public` 只作兼容镜像；
- 共享记录只有在文档处于 `shared` 时生效；
- 无权限记录、无知识库元数据时默认拒绝；
- admin 绕过只依赖服务端认证得到的 `is_admin`，不接受客户端自报角色；
- 权限变更后，后续检索和缓存来源过滤必须立即按新状态生效。
