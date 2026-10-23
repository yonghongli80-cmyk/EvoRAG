# 残余清零记录（2026-10-23）

> 前置：`2026-10-22-full-codebase-review-findings.md`、`2026-10-22-pre-upgrade-remediation-plan.md`、
> `2026-10-22-stage2-remediation-and-upgrade.md`。
> 本文只记录**本轮逐项闭环**的结果；架构重构与 Qdrant server 运行时迁移单列为计划/环境项。

## 1. 本轮清零项

| 项 | 结果 | 提交 |
| --- | --- | --- |
| R1 legacy KB 访问 | 新增管理员 KB 归属端点；4 个 KB 已归属 `ORG-81018c`，写入审计 | `da784bb` |
| R2 canary shadow 写入者 | `scripts/run_candidate_shadow.py` 跑真实 shadow 比较并写 gate；支持 dry-run | `6a28503` |
| R3 document_id 关联 | 新增 `POST /knowledge-bases/{kb_id}/documents/link`；前端改调用，不再下载再上传 | `e4fd6cb` |
| R4 声明式 schema | 整理批次回滚四列写入基线 schema；旧库幂等 ALTER 保留 | `64deae7` |
| R5 配置生效/清理 | 删 11 个无人引用字段；日志/JWT/初始管理员改由 Settings/.env 消费；JWT 保留 env > .env > 文件 | `d8a326c`/`8c352d7`/`f30749c` |
| R6 wiki job scope | `document_index_jobs.job_scope`；全新库初始化 material schema | `db59301`/`53a4325` |
| A-11 Jev 平票 | 平票项返回 `answer_evidence=0` | `a189f07` |
| A-12 客户端生命周期 | Qdrant/Reranker 懒建加锁；pipeline.close 释放 Reranker Session | `25f574b` |
| B-09 登录加固 | 用户名/密码长度上限；登录节流 map 有界；代理 `x-forwarded-proto` 决定 Secure | `6a02a5e` |
| B-10 组织写入 | 组织编号/敏感域预校验（非法 400，不再 CHECK 500）；审计 changed 字段 | `636ca72` |
| B-11 图表鉴权 | 移除匿名 `/data/charts` StaticFiles，改为登录后下载路由 | `2eff1c8` |
| C-09 mode-only 补丁 | `diff --git` 路径纳入边界校验，mode-only 不再绕过 | `93e71d9` |
| C-12 自由文本上限 | `rationale`/`expected_effect` 上限 2000 字符 | `b52a668` |
| B-12 负向用例 | 跨用户记忆、legacy KB、API-Key 停用、错误脱敏、BatchImporter、图表鉴权、SSE/竞态等用例已随各流补齐 | 见各提交 |

## 2. 验证

| 项 | 结果 |
| --- | --- |
| 全量 pytest | **2801 passed / 1 skipped（31:30）** |
| 前端 Vitest | **299 passed / 39 files** |
| 前端 build | 成功，`static/` 已重建 |
| ruff（全树） | All checks passed |
| 服务 | `/health` 200、`/ready` 200（Redis degraded 不阻塞）、`/metrics` 管理员 200 |
| 权限 | 4 个 KB 均为 `ORG-81018c / organization`；admin 200；匿名 `/data/charts/*` 401 |
| 备份 | 前一轮 `status=verified`；D-01 路径已由启动迁移覆盖 |

## 3. 架构债与 Phase 1 环境（第 2 轮进展）

1. **架构债**：`api.py`/`pipeline.py` 体量、延迟导入、PostgreSQL/多副本属于 `UPGRADE.md`
   Phase 2/3，不是可一次性安全落地的缺陷。本轮已做两步可验证收敛：
   - 抽出 `rag/materials/supervisor.py`，`api.py` 去掉 material worker 线程/全局状态
     （875 → 823 行）；
   - 新增 `tests/test_architecture_budget.py` 冻结预算（api ≤ 830、pipeline ≤ 1760、
     延迟导入 ≤ 155），后续改动不能继续膨胀；当前实测 823 / 1758 / 150。
   剩余目标是 Phase 2/3 的 pipeline 拆分与导入上移，需要独立重构周期。
2. **Phase 1 Qdrant server 运行时迁移**：本轮复核仍阻塞——Docker Desktop 无进程、
   `docker version` 报 `npipe:////./pipe/dockerDesktopLinuxEngine` 不存在、
   `wsl -l -v` 无发行版、6333/6334 未监听。未 pull 镜像、未启动 qdrant 容器、
   未做 snapshot/双进程验收；配置面、Compose、单测已完成。启动 Docker Desktop 或提供
   Docker 主机后即可执行。
3. **产品取舍**：canary 现在要求层 2 `benchmark=passed` + 层 3 shadow 至少 1 样本；
   `run_candidate_shadow.py` 是显式管理员动作，不是自动线上 5% 采样。

## 4. 放行结论

- P0/P1/P2/P3 中**可代码修复的审查项已清零**；没有已知未修的安全/数据/正确性缺陷。
- 升级前仍需：启动 Docker 做 Phase 1 运行时迁移；按组织分配 KB 成员；执行一次完整备份/恢复演练。
