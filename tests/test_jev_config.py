from config import Settings


def test_jev_defaults_are_safe():
    configured = Settings(_env_file=None)
    assert configured.typesafe_api_key == ""
    assert configured.jev_mode == "off"
    assert configured.jev_model == "jev-latest"
    assert configured.jev_timeout_seconds == 3.0
    assert configured.jev_route_confidence == 0.75
    assert configured.jev_kb_allowlist == ""


def test_jev_rejects_unknown_mode():
    try:
        Settings(_env_file=None, jev_mode="unsafe")
    except ValueError:
        return
    raise AssertionError("invalid Jev mode must be rejected")
