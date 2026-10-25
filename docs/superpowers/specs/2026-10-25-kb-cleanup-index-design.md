# KB清理目标快照与目录查找优化

基线为已合并双推的`1d413edbb1f6f7cc1d3bd4535e4ca85795680b58`。本轮实现用户顺序计划第二阶段，只优化清理计划收集，不改实际文件删除、SQL teardown、授权/gate/tombstone或返回错误契约。

## 设计选择

现实现对目录行对应permission查版本后，在permission补充循环再次查询；外部版本/manifest的目录引用检查及嵌套目录去重按全部计划目录扫描。主代理选择调用内惰性版本缓存及Path祖先集合查询。提前批量取目标版本会改变校验顺序；全量全局版本查询又缺少目标解析所需字段；字符串前缀可能混淆兄弟目录，均不采用。

### 一次调用内的目标版本快照

- `resolve_kb_document_paths`新增可选keyword-only `version_loader: Callable[[int], list[dict]] | None = None`；默认仍调用原`user_db.list_document_versions`，其它调用者行为完全保持。
- `collect_kb_cleanup`创建局部字典和局部loader，首次遇到permission时调用原list_document_versions，之后复用同一列表（包括空列表）。向KB-wide resolver注入loader，后续permission补充循环也使用它。每个目标permission在单次collect最多一次版本查询；下次collect重新加载，不持久化、不跨请求共享。
- loader定义不执行SQL，首次调用点和原校验先后保持：manifest identity/source检查仍先于文档版本路径。保留现有全局permissions/versions/manifests加载及所有外部引用校验，不用局部缓存替代全局身份检查。
- 版本列表原排序、行数据及路径校验保持；不改KB gate假设和事务拓扑。代价是单次操作保留目标版本行引用，遵循原有gate排斥目标KB写者的约束。

### 目录查询

- `_path_references_cleanup_target`保持空值、相对路径default_root、absolute与resolve双候选及resolve异常fallback、planned_paths文件优先检查；目录包含改为候选自身或其任一parent在planned_dirs集合中，不按所有目录扫描。
- 新私有`_minimal_cleanup_dirs(paths: list[Path])->list[Path]`执行原`sorted(set(paths), key=lambda path: len(path.parts))`，再以所有目录集合检查各路径的严格parent，移除嵌套子目录。保持原相同进程中的顺序和Path比较规则，不新增排序策略。
- 保留Path语义，包括自身、祖先、兄弟前缀、根、相对/绝对、大小写平台规则、点段及resolve失败；不做字符串startswith或取消任何链接/路径类型检查。
- 目录关联工作从O((U+M)·D)降为按候选深度查询；去重从O(D²)降为O(D·depth)。路径解析、文件树安全扫描及全局外部引用读取仍有必要成本，不宣称整个KB删除线性。

## 验收边界

真实`collect_kb_cleanup`路径的查询计数须先取得旧版RED；直接目录算法和collect集成的确定性访问计数须证明不再随D扫描，不能靠壁钟阈值。独立旧新完整计划/拒绝错误对照覆盖错owner、共享路径、归档identity/source、链接和错误文件类型、合法缺失重试及错误优先级。只规范临时路径前缀，不重排计划列表；除原实现本身的顺序外不引入新的稳定性承诺。

原helper默认调用者、单文档删除不注入缓存；外部链接/归属冲突在任何manager或文件删除前仍fail-closed。测试和基准只使用显式合成SQLite/目录，不调用真实清理API、不执行业务存储写入。新旧基线模块由root导出并hash锁定；永久回归不依赖Git或artifact源码。原工作树/环境/SDD保留，本阶段独立验收后提交本地分支，不自动部署。

既有cleanup_dirs同深度顺序依赖原生Path/set哈希，不能把某个Temp前缀下的顺序冻结成跨进程稳定承诺。独立golden保留所有成员；测试仅对该字段用冻结成员映射回本次Temp路径，再按旧sorted(set,key=len)+全对全strict-parent规则计算预期顺序，比较实际完整列表，不排序或忽略实际输出。其它列表直接按冻结golden顺序比较。基准同进程同输入的旧新完整计划必须逐值相等。
