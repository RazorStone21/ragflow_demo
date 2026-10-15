"""租户上下文：缺失即失败的 fail-closed 取租语义（CLAUDE.md 4.4 / 10.2 / NO-19）。"""

from __future__ import annotations

import copy
from typing import Any, cast

import pytest
from langchain_core.runnables import RunnableConfig

from agent_platform.core.errors import ErrorCode, UnauthorizedError
from agent_platform.tenants.context import TENANT_ID_CONFIG_KEY, tenant_id_from_config


def _config(tenant: object) -> RunnableConfig:
    """按 API 层的真实写入形态构造运行配置（字面键名 "tenant_id"）。"""
    return cast(RunnableConfig, {"configurable": {"tenant_id": tenant}})


def test_returns_tenant_id_from_configurable() -> None:
    """合法 config 返回其携带的租户标识（CLAUDE.md 4.4 运行期上下文通道）。"""
    config: RunnableConfig = {"configurable": {"tenant_id": "tenant-a"}}
    assert tenant_id_from_config(config) == "tenant-a"


def test_strips_whitespace_around_tenant_id() -> None:
    """首尾空白被裁剪后返回，同一租户不得因空白形成不同命名空间/过滤值。"""
    assert tenant_id_from_config(_config("  tenant-a  ")) == "tenant-a"


def test_tenant_id_config_key_is_single_source() -> None:
    """配置键名锁定为字面量 "tenant_id"——API 层写入与节点读取共用的键名单源。"""
    assert TENANT_ID_CONFIG_KEY == "tenant_id"


@pytest.mark.parametrize(
    ("config", "canary"),
    [
        pytest.param(None, "", id="config=None-未注入运行配置"),
        pytest.param({}, "", id="config={}-空配置"),
        pytest.param({"thread_id": "thread-canary"}, "thread-canary", id="config-缺configurable键"),
        pytest.param({"configurable": None}, "", id="configurable=None-通道被置空"),
        pytest.param(
            {"configurable": {"user_id": "user-canary"}},
            "user-canary",
            id="configurable-缺tenant_id键",
        ),
        pytest.param(_config(None), "", id="tenant_id=None-值为None"),
        pytest.param(_config(""), "", id="tenant_id=''-空字符串"),
        pytest.param(_config("   "), "", id="tenant_id='   '-纯空白串"),
        pytest.param(_config(123), "", id="tenant_id=123-非字符串int"),
        pytest.param(_config({"name": "dict-canary"}), "dict-canary", id="tenant_id=dict-非字符串"),
        pytest.param(_config(["list-canary"]), "list-canary", id="tenant_id=list-非字符串"),
        pytest.param(_config(True), "", id="tenant_id=True-非字符串bool"),
        pytest.param(
            {"configurable": "tenant-canary"},
            "tenant-canary",
            id="configurable='tenant-canary'-非dict形态被拒",
        ),
        # config 自身的非 dict 形态：只兜 falsy 的实现会在这里抛 AttributeError，
        # 把本应 401 的认证失败变成 500（fail-closed 仍成立，但错误映射错了）
        pytest.param("config-canary", "config-canary", id="config=字符串-非dict形态被拒"),
        pytest.param(["config-canary"], "config-canary", id="config=list-非dict形态被拒"),
    ],
)
def test_invalid_tenant_context_fails_closed(config: object, canary: str) -> None:
    """缺失或非法租户上下文一律抛 UnauthorizedError，绝不回退默认租户（NO-19）。"""
    with pytest.raises(UnauthorizedError) as excinfo:
        tenant_id_from_config(cast("RunnableConfig | None", config))

    exc = excinfo.value
    assert type(exc) is UnauthorizedError  # 精确类型：不接受任何子类或兜底异常混入
    assert exc.code == ErrorCode.UNAUTHORIZED
    assert exc.http_status == 401
    if canary:
        assert canary not in exc.detail
        assert canary not in exc.message


def test_unauthorized_problem_details_are_sanitized() -> None:
    """取租失败的 Problem Details 为 401/UNAUTHORIZED 且不回显原始配置（CLAUDE.md 8.2）。"""
    with pytest.raises(UnauthorizedError) as excinfo:
        tenant_id_from_config(_config({"api_key": "canary-secret"}))

    problem = excinfo.value.to_problem_details(request_id="req-1")
    assert problem.status == 401
    assert problem.code == ErrorCode.UNAUTHORIZED.value
    assert problem.detail  # 用户可理解的非空说明
    assert "canary-secret" not in problem.detail


def test_does_not_mutate_input_config() -> None:
    """只读语义：取出租户不修改传入的 config（CLAUDE.md 4.2 节点不得反向写配置）。"""
    config: dict[str, Any] = {"configurable": {"tenant_id": "  tenant-a  ", "thread_id": "t1"}}
    snapshot = copy.deepcopy(config)

    assert tenant_id_from_config(cast(RunnableConfig, config)) == "tenant-a"
    assert config == snapshot
