"""配置加载与 fail-fast 校验（CLAUDE.md 3.5）。"""

from __future__ import annotations

import pytest

from agent_platform.core.errors import ConfigurationError
from agent_platform.settings import Settings, load_settings

_REQUIRED = ("AGENT_API_BEARER_TOKEN", "POSTGRES_DSN", "LITELLM_API_KEY", "RAGFLOW_API_KEY")


def test_missing_required_env_fails_fast_with_variable_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_judge_upstream_model_defaults_empty_and_reads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """裁判上游型号只作留痕：默认空串（不写死型号），可由环境变量注入。

    默认**不能**给一个型号：那等于替没配置的环境编造"谁在判"，而留痕字段造假的
    后果是产物看上去可追溯、实际追溯不到。型号由部署配置决定（NO-03）。
    """
    monkeypatch.setenv("AGENT_API_BEARER_TOKEN", "tok")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://u:p@h:5432/d")
    monkeypatch.setenv("LITELLM_API_KEY", "lk")
    monkeypatch.setenv("RAGFLOW_API_KEY", "rk")
    monkeypatch.delenv("JUDGE_UPSTREAM_MODEL", raising=False)

    assert load_settings(env_file=None).judge_upstream_model == ""

    monkeypatch.setenv("JUDGE_UPSTREAM_MODEL", "Qwen/Qwen3.5-122B-A10B")
    assert load_settings(env_file=None).judge_upstream_model == "Qwen/Qwen3.5-122B-A10B"


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
