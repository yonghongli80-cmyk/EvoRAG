# 重构后代码审查与修复记录

审查起始时间：2026-10-23（香港时间），验收完成于 2026-10-24 凌晨。基线：`main`，`0c5d3060bd061534ec51aa6f8215f161ec401969`。

主代理负责定位、复现、代码审查和集成验收；三个 Luna 子代理仅实现已确认问题的修复及回归测试。改动保留在本地工作区，未提交或推送。

## 审查范围

覆盖后端、前端、测试、依赖、启动和部署配置。手工重点追踪认证与组织权限、文档导入与版本切换、检索与可靠性判定、同步/流式问答、引用与历史回读、Wiki、表格查询与编辑、后台任务和回收清理之间的数据流。全仓执行自动化检查，不代表每行代码均已人工阅读，也不构成对生产数据所有边界情况的穷尽证明。

## 已确认的问题及修复

| 编号 | 原故障及影响 | 修复和主要回归依据 |
|---|---|---|
| F01 | Wiki 分块直接导入 `markdown_it`，运行时依赖未声明；仅安装运行锁无法可靠启动 | 声明 `markdown-it-py` 并锁定其依赖；修正运行锁缺失的 `setuptools`。独立运行环境验证 `rag.api` 导入 |
| F02 | 无组织档案的管理员 `/me` 缺少 `is_admin`，前端隐藏管理入口 | 始终返回管理员标志；`test_review_auth_profile.py` |
| F03 | 自动创建会话、重复发送、模式切换、退出后的旧建议请求相互覆盖 | 为发送预约与异步会话操作增加代次校验并终止旧流；`chat.review.test.ts` |
| F04 | 迟到的 `/me` 响应恢复旧身份；真实 401 interceptor 影响认证清理；旧 `/files` 成功响应可覆盖新账户数据 | 请求去重、统一请求会话代次校验；过期成功响应按取消处理，过期 401 不清新账户、不跳转、不使新请求失效；同时覆盖 Axios 与 fetch 流式入口。使用真实 Axios interceptor/files store 回归 |
| F05 | Vite 代理改写 Host，而 Origin 保持开发端口，正常写操作被 CSRF 校验拒绝 | 保留原 Host；用真实临时 HTTP 后端与 Vite 代理验证合法请求和跨站拒绝 |
| F06 | 表格 Decimal 列按文本比较，极值、范围与部分分组结果错误 | SQLite Decimal 比较规则与 Decimal 分组；覆盖 `9.50 / 10.00 / 100.00` 等案例 |
| F07 | 表格查询把暂时执行错误写入缓存，底层恢复后同问仍失败 | 仅缓存成功结果；验证失败、恢复、命中成功缓存的完整顺序 |
| F08 | 流式问答按问题文本生成持久化键，重复问题丢失回答；用户消息与回答非原子提交 | 每请求唯一键，用户/回答/引用同事务；保存失败发送 error 而非成功 done |
| F09 | 同步 `/query` 保存答案但遗漏引用 | 同步问答复用原子保存接口与统一来源转换 |
| F10 | 重新生成只换答案，仍保留旧引用和可靠性信息 | 同事务替换回答、引用、可靠性；保留反馈内容并解除旧引用关联；前端同步替换来源 |
| F11 | Wiki 页/修订 ID 与文档/版本 ID 数值重合，引用回读、反馈和清理可能关联错误对象 | 引用增加来源域、知识库与名称；区分 Wiki/文档连接、权限和清理；真实 SQLite ID 碰撞回归 |
| F12 | 组织知识库编辑 API 允许管理者，后台激活却按个人 ACL 拒绝；授权撤销也可能遗漏 | 在激活写事务内复核组织角色、撤销、敏感资质和活动状态 |
| F13 | 管理员跳过版本范围，高分旧版本可压过当前版本；Qdrant 滞后标志影响可靠性 | 以 SQLite 当前指针和状态构建版本范围，并规范化返回块；保留确认过期内容的历史回退 |
| F14 | 清理先标记 purged 再删文件，文件删除失败后无法重试 | 文件清理成功后提交最终状态；失败保留数据集元数据与重试依据。直接传入的向量清理失败也会传播；后台逐版本向量清理沿用任务重试 |
| F15 | 默认 UserDB 路径字段名错误；健康检查探测旧 analysis 数据库并产生空库 | 统一使用 `users_db_path`，健康/就绪检查只读探测同一数据库 |
| F16 | 数据源跨域重定向继续携带调用者认证头、Cookie、auth 和查询参数 | 跨来源后移除凭证，后续跳回原域也不恢复；各跳继续校验 SSRF |
| F17 | 全新环境首次上传走版本化写入，默认 `rag_docs` 集合未创建，上传返回 400 | 真实服务复现后修复默认集合的首次写入初始化；不存在的自定义知识库仍拒绝写入 |

依赖另行更新：前端 Vue、Axios、DOMPurify 等兼容版本；Python aiohttp、banks、Brotli、certifi、langgraph-sdk、multidict、Pillow、PyJWT、urllib3 及必要传递依赖。保持现有架构，未通过放宽权限、吞掉错误或降低架构测试阈值来通过测试。

## 本地环境

- Python 3.12.12：`.venv/Scripts/python.exe`，开发依赖按锁安装，包兼容性检查通过。
- Node 22.23.4：`.venv/node-runtime/node_modules/node/bin/node.exe`。前端依赖已安装，生产文件输出到 `static/`。
- 另建只安装 `requirements.lock` 的隔离环境，149 个包，包兼容性检查及 `rag.api` 导入通过。
- `.env` 已配置用户提供的生成与向量服务。生成模型经网关 `/models` 确认为 `deepseek/deepseek-v4.1-flash`；Embedding 为 `BAAI/bge-m3`，维度 1024；Rerank 使用配置默认的 `BAAI/bge-reranker-v2-m3`。
- 凭证仅存于被 Git 忽略的 `.env`。报告、测试、构建产物均不包含密钥。

从仓库根目录启动：

```powershell
.\.venv\Scripts\python.exe start.py
```

默认地址 `http://localhost:8080`。本轮验证使用临时用户、数据库、上传目录和向量库，未向业务资料库添加测试内容。

## 验证结果

后台、前端及真实服务全链路验收完成。以下为最终代码的验证结果：

- 后台：**2932 passed, 4 skipped**，耗时 500.86 秒，退出码 0；使用 Python 3.12、8 个隔离 pytest worker。完整运行发生在后台代码和夹具停止变动之后。
- 前端：**41 个文件，316 项测试通过**，耗时 10.23 秒，退出码 0；Node 22 下类型检查与生产构建通过，最终 `static/` 已重新生成。
- 全仓 `ruff check .` 和 `git diff --check` 通过；开发环境 `uv pip check` 通过；最终凭证扫描没有在待交付源码、测试、报告或构建产物中发现本地密钥。
- 实际 FastAPI 生命周期：就绪、健康、登录、管理员资料、同源写入、跨站拒绝、退出后 401 通过。
- 真实 DeepSeek：同步生成、异步流式生成、模型池流式调用通过。
- 真实 API 生成/保存：使用固定证据，验证同步回答、重复问题流式回答、引用历史回读、重新生成后引用替换通过。
- 真实 Embedding 返回 1024 维；真实 Rerank 返回有效评分。
- 真实完整 RAG：临时 SQLite/Qdrant 中首次上传、同名覆盖为第 2 版、真实 Embedding/Rerank/DeepSeek 流式问答、同问重复保存、引用历史回读均通过。第 2 版回答正确使用顾宁/8426；首位引用为当前版本，补充旧版引用明确标为历史。没有替换检索器或模拟模型响应。
- 前端生产依赖审计：0 个已知漏洞。
- Python OSV 审计：剩余 NLTK 1 个已知且暂无修复版本的漏洞，详见下面限制。

本地验证脚本及完整日志保存在 Git 忽略目录 `artifacts/review-2026-10-23/`。实时模型脚本会发起外部请求；普通 pytest 使用测试配置，不使用本地真实密钥。

主要证据文件：`backend-test-integrated.log`、`frontend-test-integrated.log`、`frontend-build-integrated.log`、`live-rag-e2e-verified.log`、`live-query-smoke.log`、`runtime-smoke-final.log`、`runtime-lock-check.log`、`python-audit-final.json`、`frontend-audit-final.log`。早期失败日志也保留，避免把初次复现和最终通过结果混在一起。

复验命令（仓库根目录；并行运行需要本地已安装的 `pytest-xdist`）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -n 8 --dist loadfile --tb=short -ra
.\.venv\Scripts\python.exe -m ruff check .
git diff --check
```

前端标准命令为 `npm test -- --run` 和 `npm run build`，本轮通过显式指定本地 Node 22 执行。没有把仅用于本轮加速测试的 `pytest-xdist` 加入产品依赖锁。

## 剩余限制

以下为本轮结束时的限制，2026-10-24 的处理进展见 [Wiki 与自进化复查报告](2026-10-24-wiki-evolution-review.md)。上文的测试数字保留为 2026-10-23 当次证据。

1. 当时 Python 间接依赖 NLTK 3.10.3 留有 [GHSA-8mgp-746c-j5xp](https://github.com/nltk/nltk/security/advisories/GHSA-8mgp-746c-j5xp)，无可升级的修复版本。后续已改用独立工作流包并移除 NLTK 依赖路径；最终生产锁 127 个包的 OSV 扫描为 0 个已知漏洞，未添加忽略规则。
2. 老数据库里缺少来源域信息的 Wiki 引用不能仅凭数字 ID 可靠修复。后续已实现保守恢复：核对历史修订、分块、来源预览并排除 ID 碰撞；数据已删除或仍有歧义的引用保留为未决，不猜测来源。
3. 当时探测未连接到 Docker daemon，因此未执行镜像构建和容器部署验证。用户确认本机 Docker 可用后，按其指定优先级暂缓 Docker，先完成依赖、Wiki 和自进化修复。
4. 4 项既有测试跳过：Windows 账号不支持符号链接；Windows 的 `terminate()` 无法模拟 POSIX 忽略终止信号；两项真实脱敏表格数据夹具缺失。没有对用户实际生产数据和全部浏览器页面逐一验收。

版本策略说明：沿用项目已有的“当前优先，当前证据少于 `min(top_k, 3)` 时补充降权历史证据”策略。真实覆盖上传验收检查当前版本优先、历史标记正确和答案采用当前值，没有把“返回任何历史引用”误判为故障，也没有改变这项既有业务策略。
