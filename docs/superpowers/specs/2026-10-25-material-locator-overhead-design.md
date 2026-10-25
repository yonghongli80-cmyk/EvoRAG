# 材料定位全量开销优化设计

## 意图与方案选择

用户要求先合并双推本地成果，再写计划并行Luna优化材料定位，保护现有服务/业务数据。交付已完成于main/Gitee/GitHub 542a36f；本专项从该版本新建C隔离分支，root统筹。下列设计为root工程选择，用户已授权持续执行，无需重复集成/执行方式菜单。

只减少内存复制的方案简单但仍将未授权元数据全量转Python；SQL查询过滤下推可能改变KB分组顺序、Python前缀/NULL语义和全局FTS limit。选择授权名称投影到SQL、默认路径减少中间映射；查询过滤保留Python原语义。完整eager model_input及semantic允许版本集合保持，不以截断、懒求值或跨请求缓存伪造收益。

## 兼容不变量

- org policy先于admin分支，仍用现有OrgAuthorizationService READ判定；legacy admin全量，legacy非admin与get_accessible_doc_names同一名称投影（owner/public/shared，view/edit均可见，不加permission_level/删除过滤到名称来源）。同名投影语义保留，不擅改为按ID授权。
- KB顺序由旧查询未过滤live/current行的最小document id决定，KB内按id；授权或query filter不能重排。只返回旧join的未删除且有current版本记录，不顺便改现有生命周期/异常指针政策。
- 精确subject结果→FTS→semantic，document id首次命中去重，最终top_k切片；FTS仍全局top_k、原顺序，不提前按ACL重写FTS limit。semantic收到所有最终筛后当前可见版本ID，不限FTS候选或top_k。
- model_input必须在locate返回前完成，仍是全部最终docs的原三字段空格/换行格式和顺序，不限top_k；错误/空结果情况下的现有属性行为保持。
- visible_docs(db,user)两参构造注入及构造后_visible_docs覆盖都保留；模块callback monkeypatch不被快路径误吞。custom row缺current_filename时保留旧dict snapshot、版本fallback在filters之前、原字段快照/查找顺序；默认带完整current字段的路径才减少映射。
- query subject/self/0、types、updated_after的timestamp和updated_at or 0、path.startswith（大小写/通配字符/Unicode）保持Python语义，不用SQL LIKE替代。

## 授权投影接口

MaterialRepository新增：

1. `list_material_document_kb_ids() -> list[str]`：用与旧方法完全相同live/current join条件，GROUP BY KB、ORDER BY MIN(dp.id)，不带用户/query过滤。
2. `list_material_documents_for_kb(kb_id: str, *, apply_legacy_acl: bool, user_id: int | None = None) -> list[dict]`：显式bool必填；full模式仅在调用者已授权后使用。legacy模式在SQL用旧SELECT DISTINCT名称子查询，目标名称按BINARY比较以对应Python集合，不把user_id=None当full权限。投影字段和KB内id顺序等于旧full方法；target KB可用IS参数保持NULL比较安全。旧full方法保留，organizer不改行为；共享SELECT可提常量但列序/值保持。

新`rag/materials/locator_visibility.py`导出`iter_visible_material_documents(db, user) -> Iterator[tuple[int, dict]]`。按KB顺序判定当次权限后取行；每KB至多一次权限判定。若所有KB均full授权，允许复用原full批量查询并按已判定KB顺序分组，避免admin/org全授权时新增K次投影SQL；任何后出现而未判定的KB不得被该分支放行。其他情况逐授权KB查询。无跨请求缓存、无SQL写、无新schema/index/依赖。

## 默认定位引擎

旧_visible_documents可保留为上述iterator的dict薄包装，维持其完整返回形状。MaterialLocator以稳定的原callback标识选择默认迭代快路径，避免构建全量visible dict再复制docs、list(items)和每doc三字段versions dict。结果按需从默认row的current字段取版本；custom兼容路径仍保存原版本快照。默认筛选一次构建docs，完全保留值/顺序；subject=None不再扫无效exact分支。无semantic时可省略未消费的allowed-ID集合，但有semantic时必须完整构建并传递。不要跳过FTS/semantic调用或提前截断诊断。

## 验收和边界

先实际RED后实现；默认路径用真实SQLite证明未授权KB/行不会提前转成Python文档、无逐文档版本查询；legacy名称子查询、org/admin、公私共享、删除/无当前/历史版本、分组顺序及同名均有回归。保留旧实际locator源码用于完整结果+model_input+semantic参数对照；custom形状与callback/error顺序另外验证，不能只比ID集合或用弱化mock当权限实现。

成对基准同一合成fixture、旧新交替，N1000/5000/10000、并发1/2、3轮、每worker10测量+3预热；补many-KB/admin小场景以验证全授权批量分支，不声称普适更少SQL。主基准semantic=None，另做完整semantic集合确定性探针，不外呼模型/HTTP/vector。保留所有样本、最大值、操作/SQL/加载行计数、完整结果等价及全表逻辑hash前后不变；资源软门禁free>=1GiB/WorkingSet<=256MiB、BelowNormal、BLAS1，不与pytest并行。host不足则保留partial，不降低门槛。旧容量尖峰不能作新优化稳定SLO基线。

本专项仅C无.env、合成Temp/专属artifact；D已交付main及原8080不重启、不部署、不读取业务库或真实材料。实现完成后独立分项及整分支复查、相关隔离回归/Ruff/生产锁API及成对测量。优化源码先保留本地可审阅，交付与运行切换分开。
