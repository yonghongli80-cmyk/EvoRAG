from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # DeepSeek 生成
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    # 部分 OpenAI 兼容提供商不用 Authorization: Bearer，而用自定义头携带密钥
    # （例如 x-api-key）。为空时保持标准 Bearer 行为。
    deepseek_auth_header: str = ""
    # 生成与改写的默认温度。0.0 = 尽量可复现（产品口径：同问同答优先）；
    # 单次调用仍显式传 temperature 时以调用方为准。
    generation_temperature: float = Field(default=0.0, ge=0.0, le=2.0)

    # 多大模型并发池配置（JSON 格式列表，支持配置多个模型供应商/Key；为空时自动使用 deepseek_*）
    llm_providers_json: str = "[]"
    llm_default_model: str = "deepseek"

    # 嵌入模型（硅基流动 bge-m3，OpenAI 兼容 API）
    bailian_api_key: str = ""
    bailian_base_url: str = "https://api.siliconflow.cn/v1"
    bailian_embed_model: str = "BAAI/bge-m3"
    embed_dimension: int = 1024

    # 重排序引擎（Rerank）配置
    rerank_provider: str = "siliconflow"
    rerank_model: str = ""
    rerank_base_url: str = ""
    rerank_api_key: str = ""
    rerank_top_k: int = 5

    # 供应商特定默认设置
    siliconflow_rerank_url: str = "https://api.siliconflow.cn/v1/rerank"
    siliconflow_rerank_model: str = "BAAI/bge-reranker-v2-m3"

    bailian_rerank_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1/reranks"
    bailian_rerank_model: str = "gte-rerank-v2"

    local_rerank_url: str = "http://127.0.0.1:8000/v1/rerank"
    local_rerank_model: str = "bge-reranker-large"

    # TypeSafe Jev（默认安全关闭；密钥只从被 Git 忽略的 .env 读取）
    typesafe_api_key: str = ""
    jev_mode: Literal["off", "shadow", "active"] = "off"
    jev_model: str = "jev-latest"
    # 超时按实测校准：稳态预判 0.3-0.9s、段落判断 0.5-0.6s、冷启动约 1.35s；
    # 1.5s 会让临界响应触发 SDK 重试并放大 P95，故取 3.0s 并保留最多 1 次重试。
    jev_timeout_seconds: float = Field(default=3.0, gt=0.0, le=10.0)
    jev_route_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    #: 预判给出的改写概率达到该阈值才改写。0.5 时普通问题改写率 0.312（目标 0.281）；
    #: 提到 0.55 只减少“临界可改写”的请求，不动路由与判定阈值。
    jev_rewrite_probability: float = Field(default=0.55, ge=0.0, le=1.0)
    # 段落判断的重复采样轮数（1 = 关闭投票）。
    # 实测（2026-10-19）：同一候选池连判 5 轮，关键块的 answer_evidence 是
    # 0.50/0.52/0.49/0.54/0.50——抖动只有 ±0.03 却横跨 0.5，于是"有没有答案证据"在同一输入上
    # 抛硬币。单次调用约 0.3s，三轮投票约 +0.6s。预判**不占**这里的预算。
    # 注意：投票只消除"同一候选池内的采样抖动"；候选池本身随检索变化时它不负责
    # （实测「r50原厂电池价格」同一池三轮全为 YES，但跨请求的池不同，结论仍会分叉）。
    jev_vote_rounds: int = Field(default=3, ge=1, le=9)
    jev_kb_allowlist: str = ""

    # Agent
    agent_max_iterations: int = 5
    memory_db_path: str = str(Path(__file__).resolve().parent / "data" / "memory.db")
    analysis_db_path: str = str(Path(__file__).resolve().parent / "data" / "analysis.db")
    users_db_path: str = str(Path(__file__).resolve().parent / "data" / "users.db")
    wiki_db_path: str = str(Path(__file__).resolve().parent / "data" / "wiki.db")

    # 鉴权
    auth_enabled: bool = True
    auth_keys: str = "{}"
    self_registration_enabled: bool = False
    allow_initial_passwords_remote: bool = False
    login_failure_limit: int = Field(default=5, ge=1)
    login_failure_window_seconds: int = Field(default=900, ge=1)

    # 外部数据源可能携带连接凭据；未显式开启前不暴露相关接口。
    data_sources_enabled: bool = False

    # Qdrant 本地模式
    qdrant_path: str = str(Path(__file__).resolve().parent / "qdrant_data")

    # BM25 倒排索引
    bm25_db_path: str = str(Path(__file__).resolve().parent / "data" / "bm25_index.db")

    # Redis 缓存
    redis_url: str = "redis://localhost:6379/0"

    # 文档版本存储（可信知识闭环）
    document_versions_root: str = str(Path(__file__).resolve().parent / "data" / "document_versions")
    structured_datasets_root: str = str(
        Path(__file__).resolve().parent / "data" / "structured_datasets"
    )
    spreadsheet_max_bytes: int = 50 * 1024 * 1024
    spreadsheet_max_sheets: int = 50
    spreadsheet_max_rows: int = 200_000
    spreadsheet_max_columns: int = 500
    spreadsheet_max_cell_chars: int = 32_000
    sensitive_structured_query_enabled: bool = False
    feedback_cluster_days: int = 30
    feedback_cluster_users: int = 3
    knowledge_task_due_days: int = 7
    knowledge_stale_days: int = 180
    historical_result_weight: float = 0.35

    # 回答可靠性
    reliability_min_score: float = 0.35
    comparison_max_documents: int = 5

    # 增量资料生命周期
    material_file_max_bytes: int = 50 * 1024 * 1024
    material_archive_max_bytes: int = 200 * 1024 * 1024
    material_debounce_seconds: int = 300
    material_worker_lease_seconds: int = 120
    material_job_max_attempts: int = 5
    material_job_queue_limit: int = 10_000
    material_embedding_cache_path: str = str(Path(__file__).resolve().parent / "data" / "embedding_cache.sqlite3")
    material_embedding_cache_bytes: int = 10 * 1024 * 1024 * 1024
    material_worker_enabled: bool = True
    material_worker_count: int = 2
    material_embedding_slots: int = 4
    legacy_office_enabled: bool = False
    legacy_office_launcher: str = ""
    legacy_office_timeout_seconds: int = 60
    legacy_office_max_memory_mb: int = 512

    # 权威数据备份
    backup_enabled: bool = True
    backup_root: str = str(Path(__file__).resolve().parent.parent / "RAGv3-backups")
    backup_interval_days: int = Field(default=7, ge=1)
    backup_retention_count: int = Field(default=8, ge=1)
    backup_check_interval_seconds: int = Field(default=3600, ge=1)

    # 辅助自进化（观测阶段）：默认关闭；开启也只写观测，不改变任何在线行为。
    evolution_enabled: bool = False
    evolution_db_path: str = str(Path(__file__).resolve().parent / "data" / "evolution.db")
    evolution_queue_limit: int = Field(default=256, ge=1, le=100_000)
    # 同类根因累计 threshold 次（窗口 window_days 内）即进入候选池；观测保留 retention_days 天。
    evolution_cluster_threshold: int = Field(default=3, ge=1, le=100)
    evolution_cluster_window_days: int = Field(default=30, ge=1)
    evolution_observation_retention_days: int = Field(default=90, ge=1)

    # 分块与检索
    chunk_size: int = 500
    chunk_overlap: int = 80
    top_k: int = 5
    rrf_k: int = 60

    # 文件导入防护
    max_upload_bytes: int = 50 * 1024 * 1024
    max_chunk_chars: int = 8000

    # 辅助自进化后台 worker：预算（并发/批大小/运行时长/模型调用）与繁忙暂停。
    evolution_worker_enabled: bool = True
    evolution_worker_interval_seconds: int = Field(default=900, ge=1)
    evolution_worker_run_seconds: float = Field(default=120.0, gt=0.0)
    evolution_worker_max_batch: int = Field(default=200, ge=1)
    evolution_worker_busy_load_factor: float = Field(default=2.0, gt=0.0)

    # 诊断阶段（第 3 阶段）：每轮对“无草稿的 awaiting_diagnosis_review 聚类”的模型调用
    # 预算。成功与失败的调用都计入预算，达到上限即停止本轮诊断，剩余聚类留待下轮。
    # 3：单次调用为拿到完整 JSON 需要更长的完成预算与墙钟（见下面两项），
    # 3 × 150s = 450s 与原本 5 × 90s 的最坏值相同，**单轮上界不因本次调整而变长**。
    evolution_diagnosis_model_calls: int = Field(default=3, ge=0)
    # 单次诊断调用的超时：诊断用独立客户端（不复用在线生成的断路器），该超时即注入
    # 诊断客户端；证据包含多样本时推理模型可能超过 30s，默认放宽到 90s。
    # 150s：实测把完成上限提到 16000 后，同一请求会出现 >90s 的调用（撞 90s 超时）。
    evolution_diagnosis_timeout_seconds: float = Field(default=150.0, gt=0.0)
    # 单次诊断输出的 token 上限：该提供商的 reasoning token 计入完成预算，无上限时调用
    # 可达数分钟，过小则会挤掉 JSON 正文。默认 16000：实测某条问题（提示词约 9k 字符）
    # 在 8000 下 finish_reason=length、reasoning_tokens=7942、text_tokens=0，正文只剩
    # 137 字符的半个 JSON（契约违规率实测 12/90 = 13.3%）；16000 下同一请求返回完整 JSON。
    # 该上限与上面的超时必须一起看：只放宽上限会把截断换成超时。
    evolution_diagnosis_max_tokens: int = Field(default=16000, ge=1)
    # 管理员确认标签 JSON 路径（诊断证据的最高优先级）；为空=无标签，此时草案根因只能是
    # 假设级（diagnoser 后校验强制）。
    evolution_diagnosis_labels_path: str = ""
    # 每个观测采几次样取多数（1 = 与过去完全一致，不增加成本；上限 5）。
    # 动机是实测的**方差**：同一观测、同一证据包、同一提示词，两次给出不同类别
    # （obs#8 一次 chunking_structure_damage、一次 recall_miss），单次采样在那些行上等于抛硬币。
    # 代价是模型调用数 ×samples，**按调用计入每轮预算**（工人侧 `diagnosis_unstable` 记录票不一致）。
    #: 补丁生成的单次模型调用超时。实测单次生成 42–350s，而生成器默认 90s ⇒ 会截断慢但
    #: 可能成功的调用（基线里 5 个失败有 2 个是 timeout）。取 600s；整轮仍由 run_seconds 兜住。
    evolution_patch_timeout_seconds: float = 600.0
    #: 补丁生成的上下文行数覆盖。**0 = 保持既有行为**（头 400 行、靶心窗半径 120）。
    #: 设成 N 时：每文件上限 N 行，且靶心窗半径取 N//2 —— 只调上限是扩不出窗口的，
    #: 因为窗宽是 min(N, 半径*2+1)。用于「加大上下文」这一档实验，默认不改。
    evolution_patch_context_lines: int = 0
    #: 补丁输出格式。**实测（严格同样本 6 对 × 2 次）：edits 12/12 = 100%、零重试；diff 8/12 = 66.7%。**
    #: 因此默认取 edits；要回到旧行为只需设成 diff（一个环境变量，随时可回退）。
    #: diff = 模型直接写 unified diff；
    #: edits = 模型只给「路径 + 精确 old_text + new_text」，由**我们**用 difflib 拼 diff
    #: （hunk 头与上下文天然自洽 ⇒ corrupt_patch / does_not_apply 构造上不可能）。
    evolution_patch_format: str = "edits"
    #: 一键应用前跑什么测试。**full = 全量套件**（默认：补丁自带的 test_paths 是**模型自己挑的**，
    #: 拦不住"改坏别处"，这正是屎山的主要来源）；target = 只跑目标测试（快，但风险自负）。
    evolution_apply_test_scope: str = "full"
    #: 检索前改写时带上最近几轮对话（补全"它呢？/那这个多少钱"这类指代）。
    #: **默认 0 = 关闭**，与既有行为逐字节一致；开启后必须先测检索质量再谈提升。
    query_rewrite_history_turns: int = 0
    #: 改写结果按 (问题, 历史签名) 缓存。只缓存**改变了问题**的非空结果：一次瞬时模型故障
    #: 返回原句时不会被固化，下次仍会重试。缓存是进程内的，重启即失效。
    query_rewrite_cache_enabled: bool = True
    query_rewrite_cache_size: int = Field(default=512, ge=1)
    #: 双路重排：改写句与原问题各重排一次，按块取较高分（2026-10-21 实测：坏改写会把
    #: "帮我找资料"变成"如何查询资料"，同一候选池的重排分从 0.7 掉到 0.03，恰好跨过
    #: reliability_min_score）。always=每条改写请求都做（最稳，多一次重排调用）；
    #: below_threshold=仅当改写句最高分低于 rewrite_dual_rerank_threshold 时补一次；off=旧行为。
    rewrite_dual_rerank_mode: Literal["always", "below_threshold", "off"] = "below_threshold"
    rewrite_dual_rerank_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    evolution_diagnosis_samples: int = Field(default=1, ge=1, le=5)
    # 代码补丁沙箱的**基准回放**用的只读数据快照目录；为空=不给。沙箱子进程会剥掉所有
    # RAG_* 设置以保持隔离，所以回放此前连语料都拿不到、只能报 "replay unavailable"。该目录
    # 里按需放：`qdrant_data/`（语料副本）、`users.db`（ACL/主体）、`memory.db`。
    # 只注入给**回放**这一条命令：目标测试仍跑在剥离后的真空环境里。
    evolution_sandbox_data_snapshot: str = ""
    # 代码补丁沙箱回放用的**黄金 fixture**（四个出网 seam 的录制）。为空 = 不注入：回放仍会
    # 因无凭据而不可用（方案 C 的行为）。设置后只注入给**回放**这一条命令，且注入前会做
    # 过期判定（语料指纹 + seam 模块源码哈希）：过期就不注入并报"不可用"，绝不给出通过的结论。
    evolution_sandbox_replay_fixture: str = ""
    # 回放用的主体 id（0 = 不传 --user-id）。实测：不传主体时每条请求都在检索前被回答，
    # prep P50=0.0ms、route accuracy 0.15，回放"跑了但什么都没量到"——这类空转必须避免，
    # 否则层 2/3 的"无回归"是假的。
    evolution_sandbox_replay_user_id: int = Field(default=0, ge=0)
    # 代码补丁沙箱**不是安全边界**：worktree 只隔离 VCS 状态，子进程仍以宿主身份运行、可联网、
    # 可读宿主能读的一切（含 .env 与 data/）。默认 fail-closed：没有真实隔离标记
    # （RAG_EVOLUTION_SANDBOX_ISOLATED=1，由容器/低权运行器等隔离启动器在边界内设置）时，
    # run_patch_sandbox 直接拒绝，**不执行候选代码**。置 true = 操作者显式承担以宿主权限执行
    # 候选代码的风险（仅限受信候选、本机调试）；推荐用容器 + 只读挂载 + 无出网 + 独立 HOME。
    evolution_sandbox_allow_unisolated: bool = False

    # 候选阶段（第 4 阶段）：候选生成默认关闭（与观测总开关独立，需显式开启）；
    # 模型调用预算沿用第 3 阶段诊断通道（次/轮）。
    evolution_candidate_enabled: bool = False
    evolution_candidate_model_calls: int = Field(default=2, ge=0)
    # 灰度百分比配置上限：请求的百分比超过它即 400（在线路径默认 0%，不配置即不允许放量）。
    # 层 4 未批准时在线请求 100% 走当前问题集；回滚 = 秒级置 0 + 审计。
    evolution_candidate_canary_percent: int = Field(default=0, ge=0, le=100)
    # 第 4 阶段层 3 影子对比的默认采样率（每请求的确定性抽样比例）。
    evolution_candidate_shadow_sample_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    evolution_candidate_timeout_seconds: float = Field(default=120.0, ge=1.0)
    evolution_candidate_max_tokens: int = Field(default=16000, ge=1)

    model_config = {
        "env_prefix": "RAG_",
        "env_file": str(Path(__file__).resolve().parent / ".env"),
        "env_file_encoding": "utf-8",
    }


settings = Settings()
