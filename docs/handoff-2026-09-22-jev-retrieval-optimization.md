# Jev 检索链路优化交接说明

## 1. 接手目标

在不影响现有 1002 端口应用的前提下，为 RAGv3 接入 TypeSafe Jev，优化当前检索链路的生成前准备速度，并为后续规则增长和后台自进化提供统一的快速结构化判断层。

本阶段采用两次 Jev 调用：

1. 检索前批量判断意图、复杂度和是否需要查询改写。
2. ACL 权限过滤后，并行判断候选块的相关性、答案证据、前提冲突和提示注入。

现有 reranker 和路由链路必须保留为故障降级后备。普通 RAG 不再无条件调用 DeepSeek 改写；最多允许一次改写重试。

## 2. 远程仓库与分支

- Gitee：`git@gitee.com:li-yonghong111/ragv3.git`
- 基线分支：`main`
- 实现分支：`feature/jev-retrieval-optimization`
- 当前远程提交：`678bac10e01b01bc7540b60e9709cf9023b99eb9`

接手命令：

```bash
git clone git@gitee.com:li-yonghong111/ragv3.git
cd ragv3
git checkout feature/jev-retrieval-optimization
```

若 Gitee 默认 SSH 未选择正确密钥，本机已验证可用的密钥文件名为 `id_ed25519_gitee_jindian_v2`。不要提交 SSH 配置或任何私钥。

## 3. 必读文档

按以下顺序阅读：

1. `docs/superpowers/specs/2026-09-21-jev-retrieval-optimization-design.md`
2. `docs/superpowers/plans/2026-09-21-jev-retrieval-optimization.md`
3. `docs/superpowers/specs/2026-09-21-assisted-self-evolution-design.md`

Jev 设计是本阶段的约束来源；实施计划共 10 个任务，要求 TDD、逐任务提交和最终 A/B 性能验收。自进化设计只用于理解后续接口，不在本阶段实现。

## 4. 当前状态

已经完成并推送：

- 人工主导的后台自进化设计。
- Jev 检索优化正式设计。
- Jev 详细实施计划。
- Gitee `main` 和功能分支建立。

尚未推送任何 Jev 产品代码。上一台机器的隔离 worktree 中存在未完成的 Task 1 本地修改，但这些修改没有进入远程分支；新 Agent 应以远程功能分支为准，从 Task 1 正式执行，不要假定那些本地修改可用。

## 5. TypeSafe 与依赖配置

TypeSafe skill 已在原 Codex 环境安装。新 Agent 若没有该 skill，应从官方仓库安装或直接阅读：

- `https://github.com/typesafe-ai/skills/blob/main/skills/typesafe-ai/SKILL.md`
- `https://docs.typesafe.ai/llms.txt`

计划使用：

```toml
typesafe-sdk = ">=0.7,<0.8"
```

如果安装速度慢，单次命令使用清华镜像，不修改全局 pip 配置：

```bash
python -m pip install -e . --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

TypeSafe API Key 只能保存在被 Git 忽略的 `.env` 中：

```dotenv
RAG_TYPESAFE_API_KEY=<rotated-key>
```

原密钥曾出现在聊天记录中，正式使用前应在 TypeSafe 控制台轮换。禁止把密钥写入代码、测试、日志、报告、执行轨迹或提交历史。

## 6. 不可破坏的安全边界

- 权限、文件存在性、精确计算和最终动作始终由确定性代码处理。
- ACL 必须在第二次 Jev 调用之前完成；不可见文本不得发送给 TypeSafe。
- Jev 低置信度不得直接进入高风险工具路线。
- 提示注入块即使包含答案，也必须排除。
- Jev 超时、限流、鉴权失败或无效响应时，降级到旧路由和旧 reranker。
- `score_source` 必须明确区分 `jev`、`reranker` 和 `unavailable`。
- 查询改写最多重试一次，禁止循环调用。
- 不得降低既定验收阈值来让报告通过。

## 7. 隔离开发要求

建议新 Agent 创建自己的独立 worktree：

```text
branch: feature/jev-retrieval-optimization
development service port: 1102
existing stable service port: 1002
```

如果该分支已被本地 checkout，可从远程分支创建新的工作分支，例如：

```bash
git checkout -b feature/jev-retrieval-optimization-cloud gitee/feature/jev-retrieval-optimization
```

不要停止或覆盖 1002 服务。开发版本只在 1102 启动，A/B 测试使用相同问题集分别请求旧、新链路。

## 8. 基线测试记录

上一台机器在实现前得到：

- 前端：`168 passed`。
- 后端相关测试：`96 passed, 2 failed`。

两项后端失败都发生在旧 Agent 初始化阶段，原因是隔离 worktree 没有 DeepSeek API Key，OpenAI 兼容客户端拒绝空凭据；失败与 Jev 代码无关。新 Agent 配置本机忽略的 DeepSeek 凭据后，应重新运行这两项测试，不能永久忽略。

已知更早的全仓库基线问题：

- `tests/test_data_sources.py` 中存在与 DNS/SQLite 测试假设相关的失败。
- `tests/test_spreadsheet_extraction.py` 独立收集时存在 `materials.worker` 与 `spreadsheets.extractor` 循环导入。

不要把这些既有问题错误归因于 Jev，也不要为了 Jev 工作顺手修改无关模块。

## 9. 执行顺序

严格按照实施计划执行：

1. Jev 配置与 SDK 依赖。
2. 内部不可变模型和版本化问题集。
3. 容错 TypeSafe 适配器。
4. 置信度门控的预判路由。
5. 候选块策略与 Jev 分数来源。
6. 双阶段接入 `RAGPipeline`。
7. 阶段耗时追踪和非阻塞影子评测。
8. A/B 基准工具和固定问题集。
9. 完整验证及 1102 实测。
10. 最终代码审查和人工交付。

每个任务应先写失败测试，再实现最小代码、运行聚焦测试、自审并提交。不要把多个高度耦合任务并行修改到同一 worktree。

## 10. 性能验收线

- 生成前准备阶段 P50 至少降低 30%。
- P95 至少降低 20%。
- 普通问题的 DeepSeek 查询改写调用减少 70% 以上。
- 路由准确率不低于 95%。
- Top-5 证据命中率相对旧链路下降不超过 2 个百分点。
- 权限和提示注入测试全部通过。
- Jev 故障时旧链路降级测试全部通过。

报告必须包含样本数、预热次数、P50、P95、路由准确率、Top-1/Top-5、改写率、外部调用数和失败案例。不能只展示单次最快结果。

## 11. 首个执行任务

从实施计划 Task 1 开始，仅修改：

- `pyproject.toml`
- `config.py`
- `tests/test_jev_config.py`

默认必须是安全关闭状态：

```python
typesafe_api_key = ""
jev_mode = "off"
jev_model = "jev-latest"
jev_timeout_seconds = 1.5
jev_route_confidence = 0.75
jev_kb_allowlist = ""
```

完成 Task 1 后先提交和审查，再开始问题集及适配器，不要直接跳到修改 `rag/pipeline.py`。

## 12. 最终交付门禁

完成后不得自动合并到 `main`，不得切换 1002 服务，也不得删除实现 worktree。应先向管理员提交：

- 提交范围和测试结果。
- 旧链路与 Jev 链路的实测速度对比。
- 准确率、安全和降级结果。
- 已知限制。
- 关闭 Jev 或回退分支的明确操作。

只有管理员确认验收证据后，才能合并和切换稳定服务。
