# Jev 检索链路 A/B 基准

本文件说明如何安全地运行「旧链路 vs Jev 链路」的生成前准备基准，以及如何解读验收阈值。
基准只测量生成前准备阶段；答案生成不计入验收数字，可选的端到端计时单独记录。

## 1. 准备

1. 在隔离工作区（端口 1102）的本机 `.env` 中配置被 Git 忽略的凭据：

   ```dotenv
   RAG_TYPESAFE_API_KEY=<在 TypeSafe 控制台轮换后的密钥>
   RAG_JEV_MODE=active
   RAG_JEV_KB_ALLOWLIST=<本次实测的知识库 ID，逗号分隔>
   ```

   聊天记录中暴露过的旧密钥不得再使用；先在控制台轮换，再写入本机 `.env`。
   确认忽略状态：`git check-ignore -v .env`，且 `git status --short -- .env` 为空。

2. 确认用例集没有密钥，且期望来源与实际知识库内容一致：

   ```bash
   python -c "import json;print(len(json.load(open('benchmarks/jev_retrieval_cases.json'))['cases']))"
   ```

   结构化、工具、冲突与注入类用例带有 notes：实测前需管理员确认对应知识库、
   结构化数据集或成对授权文档，并按需更新 `expected_source_names`。
   不得为了拿到通过报告而删除失败用例或修改期望标签（见实施计划 Task 9 Step 7）。

## 2. 运行

```bash
python scripts/benchmark_jev_retrieval.py \
  --cases benchmarks/jev_retrieval_cases.json \
  --runs 5 --warmups 1 \
  --json-out artifacts/jev-benchmark.json \
  --markdown-out artifacts/jev-benchmark.md
```

- 同一进程内对每个用例交替执行旧、新两条准备路径（顺序逐轮反转），降低时段偏差。
- 预热调用不计入指标。
- `--include-questions` 才会在报告中包含问题文本；默认脱敏。
- `artifacts/` 已被 Git 忽略；报告与日志不得提交。

启动隔离服务（可选，用于人工抽查询问）：

```bash
python -m uvicorn rag.api:app --host 127.0.0.1 --port 1102
```

## 3. 验收阈值

| 指标 | 阈值 | 说明 |
| --- | --- | --- |
| 生成前准备 P50 | 相比旧链路降低 ≥ 30% | 每个用例、每条路径取预热后的样本 |
| 生成前准备 P95 | 相比旧链路降低 ≥ 20% | 同上 |
| 普通问题 DeepSeek 改写调用 | 减少 ≥ 70% | 按 `category=ordinary` 用例的改写率比较；旧链路基线为无条件改写，整体改写率同时报告 |
| 路由准确率 | ≥ 95% | 新路径；按期望路由域（rag/structured/tool/clarification）判定，记录的是路由决策而非最终答复形态 |
| Top-5 证据命中率 | 相对旧链路下降 ≤ 2 个百分点 | 仅统计声明了期望来源的用例 |
| 权限与提示注入测试 | 全部通过 | 由测试套件保证，不属于本基准 |
| Jev 故障降级测试 | 全部通过 | 由测试套件保证，不属于本基准 |

退出码：全部阈值通过为 0，任一失败为 1。不得下调阈值让报告通过。

## 3.1 本轮实测结论（2026-09-22，20 用例 × 每路径 5 次 + 1 次预热）

| 阈值 | 结果 | 数据 |
| --- | --- | --- |
| 准备 P50 ≥ -30% | **通过** | 旧 2231.2ms → 新 802.7ms（**-64.0%**） |
| 准备 P95 ≥ -20% | **通过** | 旧 3055.2ms → 新 2116.0ms（**-30.7%**） |
| 普通问题改写 ≥ -70% | **通过** | 旧 1.000 → 新 0.000（**-100%**）；整体 0.650 → 0.100 |
| 路由准确率 ≥ 95% | **通过** | 新路径 **1.000**（20/20 用例 × 5 次全部正确） |
| Top-5 下降 ≤ 2pp | **通过** | 新旧均为 1.000；Top-1 0.171 → 0.600 |
| 权限/注入/降级测试 | **通过** | 由测试套件保证 |

**总判定：PASS**（`python scripts/benchmark_jev_retrieval.py ...` 退出码 0）。

P95 曾经未达标，根因与修复（均已用数据验证，未下调任何验收阈值）：

1. **SDK 重试放大尾延迟**：实测稳态预判 0.3–0.9s、段落判断 0.5–0.6s、冷启动约 1.35s，
   而 `RAG_JEV_TIMEOUT_SECONDS=1.5` 会让临界响应触发 SDK 重试，单次调用翻倍到 ~3s。
   已将超时校准为 3.0s（配置默认值同步更新，重试仍保留最多 1 次）。
2. **段落判断批量无上限**：过采样 24 个候选 = 一次请求 96 个问题，延迟随批量线性增长。
   新增 `JevPolicy.max_passage_candidates=12`，只判断检索排序最前的候选。
3. **结构化路由的无效改写**：结构化问题回退文档检索时既不该预改写、也不该触发无证据重试
   （改写建议只适用于检索路径），已按路由门控。

### 环境修复（代码级，替代一次性脚本）

全新数据卷上仓库 fixture 只以无版本向量写入 Qdrant，`document_versions` 无登记，
企业级（版本化）检索因此返回空。已在启动流程中修复：

- `rag/api.py` 的 `_register_repo_document_version()`：仓库文件没有当前版本时，
  用应用自身的版本化摄取路径登记（系统主体 `ensure_development_user` 保证 FK 安全），
  幂等；每次启动都会执行。
- `rag/vector_store.py` 的 `delete_unversioned_points()`：分页清理同一文档的无版本历史向量点，
  避免版本化写入后出现重复块。

## 4. 报告解读

- `prep P50/P95`：准备阶段耗时的最近秩百分位。
- `route accuracy by category`：按用例类别分别给出的路由准确率，用于识别
  「本环境缺少某类数据」导致的类别性偏差，而不是掩盖它。
- `failed calls`：调用异常按用例记录，不计入耗时。
- `end-to-end P50/P95`：仅当准备调用返回了端到端计时时出现，供参考，不参与验收。

## 5. 收尾

- 报告与日志只保存在本机 `artifacts/`。
- 实测结束后如需关闭 Jev：将 `RAG_JEV_MODE` 设为 `off` 并重启服务；
  如需回退：合并前直接移除功能分支即可恢复旧链路。
- 本地暴露过的密钥应在控制台再次轮换。
