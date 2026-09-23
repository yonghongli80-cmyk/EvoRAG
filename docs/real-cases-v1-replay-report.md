# 真实问题回放对分报告（real-cases v1）

- 回放时间：2026-09-23；链路：Jev 主路径（active，kb_hr/kb_accessories/kb_after_sales 已加白名单）+ Command Code `deepseek/deepseek-v4.1-flash` 生成。
- 知识库：按 kb-layout.json 建三个测试库，灌入 13 份脱敏文档（hr 5 份 / accessories 5 份 / after-sales 3 份），文档级 public 可见。
- 旧系统表现来自 `cases/observed-outcomes.json`（脱敏审计，仅状态与来源类型；其答案不是管理员确认的正确答案，no_answer 不等于检索失败）。

## 总览

- 回放 26 条：可答 14、拒答 9、澄清 1、Agent 路径 1、未回放 1、异常 0。
- 观测产物：9 条失败观测（拒答案例自动落库）→ 3 个聚类 → 2 个聚类达到三次累积进入候选池（kb_accessories 检索类 5 条、另一 3 条）。

## 逐条对分

| 用例 | 知识库 | 新链路结果 | 来源命中 | 旧系统表现 | 新链路耗时 | 标记 |
| --- | --- | --- | --- | --- | --- | --- |
| real-001 | kb_after_sales | answerable | 候选命中 3/3 | no_answer×3 | 31413ms |  |
| real-002 | - | 未回放 | - | - | - | 无固定知识库 |
| real-003 | kb_after_sales | insufficient_evidence | 候选命中 0/2 | answered×1 | 5068ms | 待确认（候选资料存在却拒答） |
| real-004 | kb_after_sales | answerable | 候选命中 2/2；额外引用 ['customer-service-script.docx'] | no_answer×1/answered×1 | 17380ms |  |
| real-005 | kb_after_sales | answerable | 候选命中 1/1；额外引用 ['after-sales-policy.docx'] | answered×1 | 12297ms |  |
| real-006 | kb_hr | insufficient_evidence | 候选命中 0/1 | answered×1/no_answer×1 | 5378ms | 待确认（候选资料存在却拒答） |
| real-007 | kb_hr | answerable | 候选命中 1/1 | answered×1 | 14421ms |  |
| real-008 | kb_hr | agent(未置决策) | 候选来源：无（资料缺口） | no_answer×1 | 468ms |  |
| real-009 | kb_hr | answerable | 候选命中 1/1；额外引用 ['hr-recruitment-manager.docx', 'hr-specialist.docx'] | no_answer×1 | 23106ms |  |
| real-010 | kb_hr | answerable | 候选命中 1/1；额外引用 ['hr-recruitment-manager.docx', 'hr-specialist.docx'] | answered×1 | 17364ms |  |
| real-011 | kb_hr | answerable | 候选命中 1/1；额外引用 ['hr-recruitment-manager.docx', 'hr-supervisor.docx'] | answered×1 | 17654ms |  |
| real-012 | kb_hr | insufficient_evidence | 候选来源：无（资料缺口） | no_answer×1 | 33389ms |  |
| real-013 | kb_hr | answerable | 候选命中 1/1；额外引用 ['hr-inventory-template.xlsx', 'hr-recruitment-manager.docx', 'hr-specialist.docx'] | no_answer×1 | 23508ms |  |
| real-014 | kb_hr | insufficient_evidence | 候选来源：无（资料缺口） | no_answer×1 | 9848ms |  |
| real-015 | kb_accessories | answerable | 候选命中 1/1 | no_answer×1 | 7916ms |  |
| real-016 | kb_accessories | answerable | 候选来源：无（资料缺口） | answered×1 | 9496ms | ⚠️ 库内缺证据却判可答——需人工核查 |
| real-017 | kb_accessories | insufficient_evidence | 候选命中 0/1 | answered×1 | 5535ms | 待确认（候选资料存在却拒答） |
| real-018 | kb_accessories | insufficient_evidence | 候选命中 0/1 | no_answer×1 | 5687ms | 待确认（候选资料存在却拒答） |
| real-019 | kb_accessories | answerable | 候选命中 2/2；额外引用 ['camera-parameters-excerpt.xlsx'] | answered×1 | 4115ms |  |
| real-020 | kb_accessories | insufficient_evidence | 候选命中 2/2；额外引用 ['camera-parameters-excerpt.xlsx', 'lens-mold-assessment.xlsx'] | answered×1 | 11221ms | 待确认（候选资料存在却拒答） |
| real-021 | kb_accessories | answerable | 候选命中 2/2 | answered×2 | 3406ms |  |
| real-022 | kb_accessories | clarification_required | 候选来源：无（资料缺口） | answered×1 | 279ms |  |
| real-023 | kb_accessories | answerable | 候选命中 1/1 | answered×1 | 2860ms | ⚠️ 库内缺证据却判可答——需人工核查 |
| real-024 | kb_accessories | answerable | 候选命中 2/2；额外引用 ['camera-parameters-excerpt.xlsx'] | answered×1 | 4689ms |  |
| real-025 | kb_accessories | insufficient_evidence | 候选命中 0/1 | no_answer×1 | 5686ms |  |
| real-026 | kb_accessories | insufficient_evidence | 候选来源：无（资料缺口） | no_answer×1 | 6658ms |  |

## 重点发现

1. **两例“库内缺完整证据却判可答”，是头号人工核查项**：
   - **real-023（尼康 Z9 电池型号）**：交接包明确未找到可信配件证据，系统引用了 `camera-parameters-excerpt.xlsx`（仅有 Z9 机身参数）——疑似生成器把机身参数当成配件证据。
   - **real-016（镜头质检流程）**：交接包标注“暂缺独立质检 SOP（霉斑分级表不等于完整流程）”，系统引用 `lens-mold-assessment.xlsx` 判为可答——可能只拿到分级表而非完整流程。
2. **价格类正确拒答**：real-017/018（R50 电池价格）因文档价格已脱敏为 `<金额>` 而拒答——拒答门按设计工作，不是缺陷。
3. **资料缺口导致的正确拒答**：real-006/012/014/025/026 拒答与 label-review 标注的“暂缺”一致，说明拒答源于库内无证据而非检索缺陷。
4. **real-008 走了 Agent 路径**（未置决策、快速返回）：Jev 将“调一下 7 月招聘数量”判为工具类，与“结构化查询缺数据”的预期路由需管理员确认。
5. **real-022 正确澄清**（只有一个型号“r50”）。
6. **延迟显著升高**：多数用例 4–23 秒，显著高于 A/B 基准（那时用 deepseek-v4-flash）。原因是 v4.1-flash 为推理模型（reasoning token 拉长生成），叠加 Jev 两阶段。如需恢复速度，可在同平台换 `deepseek/deepseek-v4-flash-fast` 或关闭推理（已传 thinking=disabled，但该模型仍产出 reasoning token）。

## 待管理员确认

1. real-023 / real-016 的回答是否事实正确（Z9 电池型号、镜头质检流程到底该答什么、出自哪份文档）——若确认是“证据不足却作答”，即观测系统捕捉到的第一个真实质量缺陷。
2. real-012/real-020 拒答时“有来源但判无证据”是否符合预期（来源相关但证据不足）。
3. label-review 的 7 项资料缺口是否补充（招聘统计表、盘点制度、招聘 SOP、回收岗位说明书、镜头质检 SOP、Z9/X-S20 配件对应表、质检培训课件）。
4. real-008 的期望路由（structured 还是 tool）。
