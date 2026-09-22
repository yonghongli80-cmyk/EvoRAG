"""Public Jev decision facade used by the pipeline."""

from config import settings
from rag.jev.client import JevClient
from rag.jev.models import JevBatchResult, PreflightDecision
from rag.jev.policy import JevPolicy, KbCapabilities, RouteDecision, choose_route


class JevDecisionService:
    """Runs the preflight call and applies deterministic policy to the result."""

    def __init__(self, client: JevClient | None = None, policy: JevPolicy | None = None):
        self._client = client if client is not None else JevClient()
        self._policy = (
            policy
            if policy is not None
            else JevPolicy(route_confidence=settings.jev_route_confidence)
        )

    @property
    def client(self) -> JevClient:
        return self._client

    @property
    def policy(self) -> JevPolicy:
        return self._policy

    def route(self, question: str, capabilities: KbCapabilities) -> RouteDecision:
        """Return the deterministic route decision for one question."""
        state = {
            "question": question,
            "capabilities": {
                "structured_data_available": capabilities.has_structured_data,
                "tools_available": list(capabilities.allowed_tools),
            },
        }
        result: JevBatchResult[PreflightDecision] = self._client.preflight(state)
        if not result.available or result.value is None:
            return RouteDecision(
                route="legacy",
                should_rewrite=False,
                reason=f"preflight_unavailable:{result.failure_kind}",
                preflight=None,
            )
        return choose_route(result.value, capabilities=capabilities, policy=self._policy)

    def close(self) -> None:
        """Release the underlying adapter client."""
        self._client.close()


__all__ = ["JevDecisionService"]
