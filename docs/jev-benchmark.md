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
| 普通问题 DeepSeek 改写调用 | 减少 ≥ 70% | 按改写率比较，旧链路基线为无条件改写 |
| 路由准确率 | ≥ 95% | 新路径；按期望路由域（rag/structured/tool/clarification）判定 |
| Top-5 证据命中率 | 相对旧链路下降 ≤ 2 个百分点 | 仅统计声明了期望来源的用例 |
| 权限与提示注入测试 | 全部通过 | 由测试套件保证，不属于本基准 |
| Jev 故障降级测试 | 全部通过 | 由测试套件保证，不属于本基准 |

退出码：全部阈值通过为 0，任一失败为 1。不得下调阈值让报告通过。

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
