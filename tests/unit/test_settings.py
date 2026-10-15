"""配置加载与 fail-fast 校验（CLAUDE.md 3.5）。"""

from __future__ import annotations

import pytest

from agent_platform.core.errors import ConfigurationError
from agent_platform.settings import Settings, load_settings

_REQUIRED = ("AGENT_API_BEARER_TOKEN", "POSTGRES_DSN", "LITELLM_API_KEY", "RAGFLOW_API_KEY")


def test_missing_required_env_fails_fast_with_variable_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺失必填项时抛 ConfigurationError，且错误信息列出全部缺失变量名。"""
    for name in _REQUIRED:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(env_file=None)

    message = str(excinfo.value)
    for name in _REQUIRED:
        assert name in message, f"错误信息应包含缺失变量名 {name}"


def test_defaults_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认值生效；环境变量可覆盖默认值。"""
    monkeypatch.setenv("AGENT_API_BEARER_TOKEN", "tok")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://u:p@h:5432/d")
    monkeypatch.setenv("LITELLM_API_KEY", "lk")
    monkeypatch.setenv("RAGFLOW_API_KEY", "rk")
    monkeypatch.setenv("MILVUS_DIM", "1024")

    settings = load_settings(env_file=None)

    assert settings.milvus_dim == 1024
    assert settings.primary_chat_model == "primary-chat"
    assert settings.app_env == "dev"
    assert settings.agent_api_bearer_token.get_secret_value() == "tok"


def test_invalid_value_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法取值（超范围）启动即失败，并指出变量名。"""
    monkeypatch.setenv("AGENT_API_BEARER_TOKEN", "tok")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://u:p@h:5432/d")
    monkeypatch.setenv("LITELLM_API_KEY", "lk")
    monkeypatch.setenv("RAGFLOW_API_KEY", "rk")
    monkeypatch.setenv("SIMILARITY_THRESHOLD", "3.5")  # 允许范围 0~1

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(env_file=None)

    assert "SIMILARITY_THRESHOLD" in str(excinfo.value)


def test_secrets_are_secretstr(fake_settings: Settings) -> None:
    """密钥字段为 SecretStr，直接 repr 不泄露明文（CLAUDE.md 10.1）。"""
    assert "secret" not in repr(fake_settings)  # postgres 密码不出现在 repr
    assert fake_settings.postgres_dsn.get_secret_value().endswith("test_db")
