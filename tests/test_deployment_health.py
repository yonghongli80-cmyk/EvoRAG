"""Deployment health checks must target the port published by Compose."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_github_deploy_health_port_matches_compose_public_port():
    """Changing only one port would make successful deployments report failure."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )

    compose_match = re.search(r'-\s*["\'](?P<host>\d+):8000["\']', compose)
    deploy_match = re.search(r"curl[^\n]+127\.0\.0\.1:(?P<host>\d+)/health", workflow)

    assert compose_match is not None
    assert deploy_match is not None
    assert deploy_match.group("host") == compose_match.group("host")
