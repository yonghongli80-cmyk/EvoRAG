"""测试认证默认开启。

安全修复：`config.py` 中 `auth_enabled` 默认应为 True（而不是 False），
避免 .env 未配置 RAG_AUTH_ENABLED 时整个服务以匿名方式对外暴露。
"""


def test_auth_enabled_defaults_to_true_when_env_unset(monkeypatch):
    """环境变量 RAG_AUTH_ENABLED 未设置时，新建 Settings() 的 auth_enabled 应为 True。

    conftest 为了既有测试继续在认证关闭下运行，会把 RAG_AUTH_ENABLED 预置为 "false"。
    因此本测试用 monkeypatch.delenv 显式恢复"未设置"状态，再新建实例独立验证默认值。
    pydantic-settings 会在 import config 时实例化模块级 settings，
    所以必须用新建 Settings() 实例来测默认值，而不能读模块级 settings。
    """
    monkeypatch.delenv("RAG_AUTH_ENABLED", raising=False)

    from config import Settings

    s = Settings()
    assert s.auth_enabled is True
