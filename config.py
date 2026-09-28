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

    # 嵌入模型（硅基流动 bge-m3，OpenAI 兼容 API）
    bailian_api_key: str = ""
    bailian_base_url: str = "https://api.siliconflow.cn/v1"
    bailian_embed_model: str = "BAAI/bge-m3"
    embed_dimension: int = 1024

    # 百炼 Rerank
    bailian_rerank_model: str = "gte-rerank-v2"
    rerank_top_k: int = 5

    # TypeSafe Jev（默认安全关闭；密钥只从被 Git 忽略的 .env 读取）
    typesafe_api_key: str = ""
    jev_mode: Literal["off", "shadow", "active"] = "off"
    jev_model: str = "jev-latest"
    # 超时按实测校准：稳态预判 0.3-0.9s、段落判断 0.5-0.6s、冷启动约 1.35s；
    # 1.5s 会让临界响应触发 SDK 重试并放大 P95，故取 3.0s 并保留最多 1 次重试。
    jev_timeout_seconds: float = Field(default=3.0, gt=0.0, le=10.0)
    jev_route_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    jev_kb_allowlist: str = ""

    # Agent
    agent_max_iterations: int = 5
    memory_db_path: str = str(Path(__file__).resolve().parent / "data" / "memory.db")
    analysis_db_path: str = str(Path(__file__).resolve().parent / "data" / "analysis.db")
    users_db_path: str = str(Path(__file__).resolve().parent / "data" / "users.db")

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
    evolution_diagnosis_model_calls: int = Field(default=5, ge=0)
    # 单次诊断调用的超时：诊断用独立客户端（不复用在线生成的断路器），该超时即注入
    # 诊断客户端；证据包含多样本时推理模型可能超过 30s，默认放宽到 90s。
    evolution_diagnosis_timeout_seconds: float = Field(default=90.0, gt=0.0)
    # 单次诊断输出的 token 上限：该提供商的 reasoning token 计入完成预算，无上限时调用
    # 可达数分钟，过小则会挤掉 JSON 正文。**默认 8000 已知会在证据包较大时饿死正文**：
    # 实测某条问题（提示词约 9k 字符）finish_reason=length、reasoning_tokens=7942、
    # text_tokens=0，正文只剩 137 字符的半个 JSON；提到 16000 后同一请求返回完整 JSON，
    # 但会撞上 90s 诊断超时（同一批 3 次里 1 次 APITimeoutError）。放宽上限与放宽超时
    # 必须一起评估，因此默认值暂不动，取舍见 docs/evolution-diagnosis.md 第 5 节。
    evolution_diagnosis_max_tokens: int = Field(default=8000, ge=1)
    # 管理员确认标签 JSON 路径（诊断证据的最高优先级）；为空=无标签，此时草案根因只能是
    # 假设级（diagnoser 后校验强制）。
    evolution_diagnosis_labels_path: str = ""

    # 候选阶段（第 4 阶段）：候选生成默认关闭（与观测总开关独立，需显式开启）；
    # 模型调用预算沿用第 3 阶段诊断通道（次/轮）。
    evolution_candidate_enabled: bool = False
    evolution_candidate_model_calls: int = Field(default=2, ge=0)
    # 灰度百分比配置上限：请求的百分比超过它即 400（在线路径默认 0%，不配置即不允许放量）。
    # 层 4 未批准时在线请求 100% 走当前问题集；回滚 = 秒级置 0 + 审计。
    evolution_candidate_canary_percent: int = Field(default=0, ge=0, le=100)
    # 第 4 阶段层 3 影子对比的默认采样率（每请求的确定性抽样比例）。
    evolution_candidate_shadow_sample_rate: float = Field(default=0.05, ge=0.0, le=1.0)

    model_config = {
        "env_prefix": "RAG_",
        "env_file": str(Path(__file__).resolve().parent / ".env"),
        "env_file_encoding": "utf-8",
    }


settings = Settings()
