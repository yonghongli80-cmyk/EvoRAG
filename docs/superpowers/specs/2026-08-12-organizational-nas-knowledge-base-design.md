# 金典知识库：组织权限、NAS 与检索专项设计

日期：2026-08-12  
状态：待实施（审核修订版）

## 1. 最终项目与初始化边界

唯一最终开发目录为：

`C:\Users\Administrator\Desktop\金典知识库`

当前该目录为空。实施第 0 阶段必须：

1. 将 `C:\Users\Administrator\Desktop\RAGv3\RAGv3` 复制为“金典知识库”的代码底座：以 `git ls-files` 得到的当前工作树受控文件为主，并额外白名单复制本设计及三份实施计划；排除 `.git`、其他未跟踪文件、运行数据、缓存、虚拟环境、构建产物和密钥；
2. 在“金典知识库”中创建新的 Git 仓库并跑通 Python 3.12、后端、前端和数据库基线测试；
3. 从 `C:\Users\Administrator\Desktop\金典拍拍知识库` 迁移固定名单、组织树、负责人范围、敏感白名单和权限验收规则，不复制其姓名登录或内存 Demo 架构；
4. 初始化完成后，RAGv3 与“金典拍拍知识库”均只作为只读来源，后续业务代码、测试、配置和文档只修改“金典知识库”。

初始化必须在 `docs/source-baseline.md` 记录 RAGv3 的 HEAD、受控文件中相对 HEAD 的修改及其 SHA-256、额外白名单文件及其 SHA-256、明确排除项，以及从 Demo 迁入的数据/规则文件清单。这样复制的是一个可复核的“当前工作树快照”，不是含义不明的 HEAD，也不是整个 dirty 目录。

禁止直接在 RAGv3 上实施本设计。

## 2. 三个模块

1. **身份与组织 ACL**：固定名单决定组织、负责人和敏感权限；钉钉 `userid` 是可信外部身份。
2. **NAS 目录与版本同步**：只读发现文件，按受控根目录规则和最长组织路径链确定归属，复用 V3 版本/索引生命周期。
3. **权限感知知识库**：先计算授权文档集合，再下推 Qdrant、BM25 和材料 FTS；重排前后、引用重开和文件操作再次鉴权。

真实 NAS 尚未提供。首期使用本地目录适配器；后续通过系统只读挂载或实现同一 `NasSource` 协议接入，不改变 ACL 和检索核心。

### 2.1 V3 代码复用边界

- **原样复用并以回归测试锁定**：解析器、分块、embedding、OCR、表格处理、引用基础结构、文档版本存储、材料任务队列及重试机制；除适配新接口外不重写。
- **在 V3 文件上定点改造**：`rag/vector_store.py`、`rag/retriever.py`、`rag/versioned_retrieval.py`、`rag/pipeline.py`、`rag/api.py`、`rag/operations_api.py`、材料检索/归档入口、表格入口、引用与可靠性入口；改造目标是把统一 ACL 前置和动作鉴权接入现有链路。
- **全新开发**：`rag/org_acl/` 与 `rag/nas_catalog/`；这里承载固定名单、组织继承、敏感白名单、NAS 目录归属和同步编排，不从姓名登录 Demo 复制运行架构。
- **仅迁移数据与验收规则**：从“金典拍拍知识库”迁入固定名单、组织节点、负责人关系、敏感资格和账号抽查矩阵；姓名只作展示/人工核对，不进入生产鉴权。

## 3. 身份与名单模型

固定名单人员拥有独立、稳定的 `roster_person_id`，不能用姓名或测试 userid 代替：

- `roster_people(roster_person_id, display_name, employment_status, binding_status, active)`；
- `external_identities(roster_person_id, user_id, provider, external_userid, identity_kind, status, ...)`；
- `external_userid` 可为空；取得正式钉钉身份后绑定到 `roster_person_id`；
- 演示身份使用 `identity_kind='test'` 的明确测试值，不能标记成正式钉钉身份；
- 姓名只用于显示和人工核验，永不参与授权或自动绑定；
- JWT 只携带内部 `user_id`，客户端不能自报 `userid` 获权。

名单导入采用“上传快照 → 预检差异与风险 → 管理员确认 → 单事务原子激活 → 可回滚到上一快照”。预检必须显示新增、离职、转岗、负责人变化、敏感授权变化、未绑定人员以及预计失权人数。错误快照不能部分生效。

## 4. 组织树与最长路径链匹配

组织以全局唯一、稳定且不随改名变化的 `org_code` 标识。名称仅在父节点上下文中唯一：

- `org_units(org_code, parent_org_code, name, normalized_name, kind, active)`；
- 唯一约束是 `(parent_org_code, normalized_name)`，不是全公司名称唯一；
- 别名同样按 `(parent_org_code, normalized_alias)` 唯一。

路径解析不是单目录段全局查找，而是最长组织路径链匹配。例如两条链可同时存在：

- `品牌品类事业部/价格维护组`
- `平台回收事业部/价格维护组`

对每条受控根目录规则，从其组织路径起点开始，按父子关系逐段匹配规范名或别名，选择消费目录段最多的唯一链。若两条链长度相同且均可成立、父路径缺失、或出现歧义，则 `resolution_status='unresolved'` 并默认拒绝。

## 5. 受控 NAS 根目录规则

目录含义来自管理员配置，不靠大模型或模糊猜测：

| 根目录示例 | 模式 | 结果 |
|---|---|---|
| `/公司公共资料` | `public` | `owner_org_code=company_root`，文档基线可见范围为 public |
| `/部门资料/<组织路径>` | `organization` | 按最长组织路径链确定主归属 |
| `/敏感资料/财务/<组织路径?>` | `sensitive:finance` | 无组织后缀时归属 `company_root`；有后缀时归属解析出的组织；默认拒绝 |
| `/敏感资料/人事/<组织路径?>` | `sensitive:hr` | 无组织后缀时归属 `company_root`；有后缀时归属解析出的组织；默认拒绝 |
| `/敏感资料/客户/<组织路径?>` | `sensitive:customer` | 无组织后缀时归属 `company_root`；有后缀时归属解析出的组织；默认拒绝 |

规则表保存 `source_id`、规范根前缀、模式、可选起始 `org_code` 和敏感域。规则必须前缀边界匹配；重叠规则按最长前缀唯一匹配，冲突则拒绝。未命中任何规则的文件不解析正文、不建索引、不可列出。

## 6. 动作级 ACL 模型

授权动作固定为：`view`、`download`、`edit`、`delete`、`manage`。

- 每个动作独立保存和判定；运行时不把单字段 `view/edit` 当作权限模型；
- `download` 永远独立，拥有 `view` 或 `edit` 不自动获得下载；
- `manage` 只表示分享、撤权和修改 ACL，不自动包含查看、下载、编辑或删除；
- UI 的“编辑者”等预设必须在写入时展开为明确动作行，例如编辑者=`view + edit`；运行时不做隐式包含；
- `document_permissions.downloadable=false` 是文档级全局下载拒绝，优先于 user/org allow，owner/admin 也不能通过普通下载接口绕过；只有先由有 `manage` 权限者改为 true 才可下载。

统一规则表：

`document_acl_rules(id, document_id, subject_type, subject_id, action, effect, expires_at, granted_by, active, created_at)`

- `subject_type` 为 `user`、`org` 或 `roster_person`；
- `effect` 为 `allow` 或 `deny`；
- 同一请求中，匹配动作的有效 `deny` 高于所有 allow、owner、public、成员关系和负责人继承；
- 到期规则不参与判断；撤权通过停用规则并写审计，不物理覆盖历史。

`document_org_acl(document_id, owner_org_code, sensitivity, exposure, resolution_status, document_acl_version)` 保存主归属与基线；跨部门/个人授权统一写 `document_acl_rules`，不再新建含糊的 `document_org_shares.permission`。

授权顺序：文档失效/未归属 → 有效显式 deny → `downloadable=false`（download）→ 敏感域资格 → 动作级 allow → owner/admin 基线 → 负责人范围 → 本部门成员 → public 基线 → 拒绝。敏感文档即使 public 或负责人继承也必须先有对应敏感资格。

## 7. 授权版本与数据库边界

- `roster_acl_version`：名单激活、身份绑定、成员、负责人或敏感资格变化时递增；
- `document_acl_version`：主归属、exposure、下载开关或文档规则变化时逐文档递增；
- 二者不得混成单个 `acl_version`；查询审计记录两个版本。
- `roster_acl_version` 只存在于授权查询上下文和审计记录，不写入每个 Qdrant point；名单变化必须立即影响授权集合，不能要求全量重建向量。
- Qdrant point 只保存稳定 `document_id`、当前内容/逻辑版本字段和逐文档 `document_acl_version`；归属变化通过该文档的 payload 刷新完成。

组织仓储不能绕过 `UserDB` 的线程锁共享 SQLite connection。所有读写通过 `UserDB` 暴露的锁保护方法或由 `UserDB` 创建的仓储完成；多表写入必须在持有 `UserDB._lock` 时进入 `UserDB.transaction()`，遵守现有单一 BEGIN/COMMIT/ROLLBACK 边界。

## 8. NAS 文件身份与移动

`DiscoveredFile` 包含 `relative_path`、内容属性和可选 `native_file_id`：

1. NAS/NTFS 能提供稳定文件 ID 时，`source_id + native_file_id` 是文件身份，移动可保留逻辑 `document_id`；
2. 无稳定文件 ID 时，`source_key` 只标识当前规范相对路径，不能宣称可识别移动；
3. 仅当旧缺失项与新项目的内容哈希在整个源内一对一唯一时，才推断移动；
4. 哈希重复、候选不唯一或跨源移动时按删除+新增处理，禁止猜测。

移动导致归属改变时，先在事务中递增 `document_acl_version` 并使旧授权失效，再异步刷新 Qdrant payload。NAS 扫描失败不能被解释为全量删除；需连续成功扫描确认缺失。

## 9. 权限感知检索

`OrgAclService.allowed_document_ids(user_id, kb_id, action='view') -> AuthorizedDocumentScope` 产生授权逻辑文档集合。`AuthorizedDocumentScope` 固定包含 `document_ids: frozenset[int]`、`roster_acl_version: int` 和 `document_acl_versions: dict[int, int]`；召回器只消费 `document_ids`，审计记录同时消费两类版本。该范围在候选生成前覆盖所有检索入口：

- Qdrant：按 `document_id MatchAny` 过滤；
- BM25：评分前只保留允许文档 chunks；
- material FTS：`search_material_search_fts(text, allowed_document_ids, limit)` 在 SQL/FTS 查询内限制文档；
- `/materials/locate`：先求允许文档，再调用 FTS 与语义检索；
- `POST /knowledge-bases/{kb_id}/query`：改为调用同一版本化 ACL 检索服务；完成迁移前不得保留先召回后过滤的旧路径。

空授权集合立即返回空。ACL 解析、Qdrant 过滤或 FTS 过滤失败均 fail closed，不退回全库检索。

Qdrant `MatchAny` 过大时按允许 ID 分批：每批用相同 query vector、过滤条件和候选深度查询；基于同一 collection/distance 的原始 dense score 合并，按 point ID 去重取最高分，全局按 score 排序后再截断。dense 与 BM25 分别完成全局候选合并后才进入 RRF；不能对各批 RRF 名次直接拼接。

最终答案缓存继续关闭。本阶段不实现 ACL fingerprint 缓存、不恢复 cache read/write；缓存单独立项，待用户与文档两条版本链稳定后评审。

## 10. 统一资源访问面

以下入口都必须以 `document_id + action` 调用同一 `OrgAclService.authorize_document()`：

- `rag/api.py`：文件/KB 操作和旧 KB 查询；
- `rag/operations_api.py`：版本列表和版本下载，下载必须检查 `download`，不能只检查 `view`；
- `rag/materials/api.py`、`locator.py`、`archives.py`：定位、归档搜索、回收、组织操作；
- `rag/spreadsheets/api.py` 及结构化查询：表格行读取和查询检查 `view`；导出检查 `download`；
- `rag/citations.py`：生成标题、摘要、预览或下载链接前实时检查；无权引用表现为不存在；
- `rag/reliability/api.py` 与比较工具：创建和重开持久化比较时逐文档实时检查；
- 历史引用重新打开、文档版本、比较结果和结构化数据都不能只依赖创建时权限。

敏感拒绝不得泄露文件名、命中数量、标题、摘要、正文预览、引用、路径或区分“存在但无权”的异常信息。对无权资源统一使用受控 404/空结果；审计日志只记录内部 ID 和 reason code。

## 11. 分阶段验收

0. 初始化“金典知识库”并锁定两个来源只读；
1. 稳定人员、身份、组织全路径、动作级 ACL、名单预检/确认/回滚；
2. 本地模拟 NAS、受控根规则、文件移动和增量版本同步；
3. Qdrant/BM25/FTS 前置过滤与全部资源入口统一鉴权。

每阶段完整回归后再继续。真实 NAS 到位后只增加挂载、只读凭据、native file ID 能力、网络中断、中文路径、大文件和扫描性能验收。
