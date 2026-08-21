from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # DeepSeek 生成
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    # 嵌入模型（硅基流动 bge-m3，OpenAI 兼容 API）
    bailian_api_key: str = ""
    bailian_base_url: str = "https://api.siliconflow.cn/v1"
    bailian_embed_model: str = "BAAI/bge-m3"
    embed_dimension: int = 1024

    # 百炼 Rerank
    bailian_rerank_model: str = "gte-rerank-v2"
    rerank_top_k: int = 5

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

    # 分块与检索
    chunk_size: int = 500
    chunk_overlap: int = 80
    top_k: int = 5
    rrf_k: int = 60

    # 文件导入防护
    max_upload_bytes: int = 50 * 1024 * 1024
    max_chunk_chars: int = 8000

    model_config = {
        "env_prefix": "RAG_",
        "env_file": str(Path(__file__).resolve().parent / ".env"),
        "env_file_encoding": "utf-8",
    }


settings = Settings()
