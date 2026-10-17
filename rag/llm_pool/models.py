"""Data structures and configuration models for the multi-LLM concurrency pool."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class LLMProviderConfig(BaseModel):
    """Configuration for an individual LLM Provider in the pool."""

    provider_id: str = Field(..., description="唯一标识符，例如 deepseek, qwen, openai")
    name: str = Field(..., description="前端显示的展示名称，例如 DeepSeek V3, 通义千问 2.5")
    model: str = Field(..., description="目标 API 模型名，例如 deepseek-chat, qwen-plus")
    base_url: str = Field(..., description="API 基础地址")
    api_key: str = Field(default="", description="API 密钥")
    auth_header: str = Field(default="", description="自定义认证头，为空则使用 Authorization: Bearer")
    weight: int = Field(default=1, ge=1, le=100, description="负载均衡权重")
    max_concurrency: int = Field(default=10, ge=1, le=1000, description="最大并发在途请求数")
    rpm_limit: int = Field(default=60, ge=1, le=100000, description="每分钟请求数限额")
    timeout_seconds: float = Field(default=30.0, gt=0.0, description="单次请求超时时间（秒）")
    is_active: bool = Field(default=True, description="是否启用")


@dataclass
class ProviderHealthState:
    """Runtime health and load state for an LLM Provider."""

    provider_id: str
    healthy: bool = True
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    active_requests: int = 0


@dataclass
class ModelResponseChunk:
    """SSE event chunk for single or multi-model streaming."""

    model: str
    content: str
    type: str = "token"
    sources: list[dict[str, Any]] = field(default_factory=list)


def load_provider_configs_from_settings(settings: Any) -> list[LLMProviderConfig]:
    """Parse configured providers from settings.llm_providers_json.

    If empty or parsing fails, gracefully synthesizes a default provider
    from settings.deepseek_* to ensure 100% backward compatibility.
    """
    raw_json = getattr(settings, "llm_providers_json", "").strip()
    configs: list[LLMProviderConfig] = []

    if raw_json and raw_json != "[]":
        try:
            items = json.loads(raw_json)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        configs.append(LLMProviderConfig(**item))
        except Exception as exc:
            logger.warning("Failed to parse llm_providers_json: %s; falling back to default.", exc)

    if not configs:
        # 兼容模式：使用现有的 deepseek_* 配置自动构建默认提供商
        default_key = getattr(settings, "deepseek_api_key", "")
        default_url = getattr(settings, "deepseek_base_url", "https://api.deepseek.com")
        default_model = getattr(settings, "deepseek_model", "deepseek-v4-flash")
        default_auth_header = getattr(settings, "deepseek_auth_header", "")

        configs.append(
            LLMProviderConfig(
                provider_id="deepseek",
                name="DeepSeek",
                model=default_model,
                base_url=default_url,
                api_key=default_key,
                auth_header=default_auth_header,
                weight=1,
                max_concurrency=10,
                rpm_limit=60,
                timeout_seconds=30.0,
                is_active=True,
            )
        )

    return configs
