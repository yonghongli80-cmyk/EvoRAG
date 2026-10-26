# 本地合同加固：失败诊断、SQLite identifier 与命令事务边界

用户要求继续并发Luna，由root主导。BASE `0b488725ab08a111ced41e7732d5d22d4d08a8ef`，复用C隔离工作树，新分支 `codex/local-contracts-2026-10-26`。上一批3462/5是历史基线，不当作本批结果。服务器正式run/P99/SLO、上线和业务迁移继续暂缓；不自动合并双推。

## 方案和root裁决

已比较：收紧canonical key正则会改变旧schema/扩展者的接受规则；直接改跨repository事务协议会扩大一致性风险。本批选择最小的失败诊断补齐、SQLite单identifier引用与操作级事务清单，保持原调用、授权和存储协议。用户已委托主导并执行，本设计的具体实现取舍由root负责，不伪称逐项获得用户设计审批。

### A. 每个受管工作失败一次明确诊断

当前失败callback可在cleanup intent之前清active，随后取消胜出，次要失败没有诊断。对非cancelled token无条件读取异常并记录一次work-failure，不再依赖cleanup_requested。普通await仍向caller抛原错误对象；取消胜出仍是原CancelledError。日志、token callback均不得向Future setter抛BaseException，logger自身KeyboardInterrupt也不能影响资源清理。

诊断注册为独立token done callback，与generation/active身份约束的资源callback分开：finish可能在资源callback之前看到token已done并清active，不能因旧资源callback早退丢日志。grace-expiry诊断本身也须隔离BaseException，不能覆盖原取消；该分支先用独立RED证实再修。两种完成先后顺序及普通错误身份均纳入focused GREEN。

保留late failure现有消息或同样精确的语义，不能把cleanup前失败称为cleanup后；默认executor/ContextVars、generation/token gate、5秒排空grace、一次finalizer、cleanup顺序、stop=False资源保护均不改。不新增日志状态机或一般worker协议。

真实Thread/Event先RED：原callback已处理failure、清active且看到无cleanup，外层wrapper再用屏障暂停；取消caller、finish后放行callback。证明主取消身份、一次清理、诊断恰一次；所有屏障finally释放。另测普通失败对象身份、logger抛BaseException及原late/loop-close路径。

### B. SQLite单identifier引用

新增纯模块 `rag/sqlite_identifiers.py`，接口 `quote_sqlite_identifier(value: str) -> str`：仅str，非str抛TypeError；NUL抛ValueError且消息不含输入；内部双引号加倍，外层双引号；空名编码为`""`，其余Unicode、空格、标点、保留字不改名、不删除、不规范化。正常parser键的SQL引用文本保持原样。它只处理一个identifier，不接受表达式或qualified name的特殊解释，不替代值参数、ACL或metadata信任合同。

只接入spreadsheet store建表/插入表列、rows API的manifest表/列、staging audit的PRAGMA表名和SELECT表/列。保留完整sorted额外列与id排序、值的?绑定、分页、公式/合并区域、只读/immutable/query_only、路径/归档身份检查。不改ConnectionManager、UserDB或SQL方言，也不新增canonical白名单、不关闭DQS兼容开关、不把内部坏数据改映射为404；既有HTTP错误契约保留。不得借此宣称任意不可信dataset已获支持或已证实应用注入漏洞。

依据[SQLite tokenizer H41150](https://sqlite.org/draft/tokenreq.html)的双引号与成对双引号规则；[DQS兼容行为](https://sqlite.org/quirks.html#double_quoted_string_literals_are_accepted)意味着单测不能只看SELECT不报错，必须创建实际对象、存入不同于名称的数据并检查真实值/完整响应。

测试使用TempSQLite实际DDL/DML及真实API/snapshot路径。先RED证明含引号合法名称原路径失败，再实现helper/接入。覆盖普通键完全不变、含双引号/Unicode/空格/空名、值参数、只读前后hash与外部对象未改变；NUL/non-str仅helper和store原子失败边界，不伪造SQLite可存NUL schema。store任意ParsedSheet测试不含不受支持的formula key；API测试可用含引号表名、合法对应列/数据及原公式/分页契约，不扩张公式坐标合同。每个源与新测试文件<1000行，原大型测试只读，新增focused模块。

### C. 操作级事务边界清单

文档精确映射KB删除、上传/索引发布、受控恢复的入口→repository/helper→连接/commit/rollback→文件/vector/private store效果。区分manager outer事务、caller-owned、直接commit、private/external与unknown；写出当前补偿与未覆盖原子性、最小合成故障矩阵及后续UoW顺序。不是完整writer证明，不增加空壳UoW、不改CM/业务事务。顺手更新review_dynamic_sql模块摘要描述已有evidence来源检查，纯docstring不写镜像测试。

## 集成和保护

现有Dmain `99834ca`、PID48512及真实.env仅核对哈希；不读业务资料/DB/密钥，不重启部署、正式压力/模型外呼。沿用私有dev/runtime环境，无新依赖。pytest一次一个、BelowNormal、BLAS1、offline guard、新OS短basetemp，不修改pytest TEMP/TMP。root唯一Git/计时/记录操作者，子代理不得timer/submit或再派代理。所有旧工作树、环境、SDD、失败原件保留。

代码分项和整分支独立复审；最后冻结后一次完整后端及生产锁烟测，前后源码核对。B会改变SQL findings的行号、hash和rationale依据，必须新建inventory并逐项重新审查受影响finding/evidence；未改文件只在identity/hash都一致时保留原处置，不能盲目给旧seed补新hash。writer候选新建，PG/完整writer声明保持未验。完成实际公司记录submit及非DB归档，保留local分支。
