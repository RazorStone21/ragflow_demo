"""会话资源与鉴权测试（CLAUDE.md 8.1 / 8.2 / 10.2 / NO-19）。

会话的资源语义刻意与 LangGraph Server 对齐：``POST /threads`` **只发号**
（不写库、不建表），会话在其首次 run 因 checkpoint 落库而真正存在。
因此"刚创建就 GET"必须是 404，而不是返回一个空壳资源——那会让调用方
以为会话已有可用状态。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_platform.core.ids import compose_thread_id, parse_thread_id
from agent_platform.main import API_PREFIX

if TYPE_CHECKING:
    from tests.conftest import ApiHarness


async def test_create_thread_returns_201_with_tenant_scoped_id(api: ApiHarness) -> None:
    """新建会话返回 201，其 ID 的租户/用户段来自服务端配置，而非请求体（NO-19）。"""
    response = await api.client.post(f"{API_PREFIX}/threads", headers=api.auth_headers)

    assert response.status_code == 201
    body = response.json()
    tenant_id, user_id, session_id = parse_thread_id(body["thread_id"])
    assert tenant_id == api.stack.settings.demo_tenant_id
    assert user_id == api.stack.settings.demo_user_id
    assert session_id
    # 创建时不产生任何 checkpoint：字段如实为 null/空，不编造
    assert body["latest_checkpoint_id"] is None
    assert body["updated_at"] is None
    assert body["next"] == []
    assert body["has_interrupt"] is False


async def test_create_thread_issues_distinct_ids(api: ApiHarness) -> None:
    """每次创建都是新会话（session_id 唯一），不得复用。"""
    ids = set()
    for _ in range(3):
        response = await api.client.post(f"{API_PREFIX}/threads", headers=api.auth_headers)
        ids.add(response.json()["thread_id"])

    assert len(ids) == 3


async def test_get_thread_before_first_run_is_404(api: ApiHarness) -> None:
    """刚创建、尚未 run 的会话 GET 为 404（会话的存在性由首次 checkpoint 定义）。"""
    thread_id = await api.create_thread()

    response = await api.client.get(f"{API_PREFIX}/threads/{thread_id}", headers=api.auth_headers)

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "NOT_FOUND"


async def test_get_thread_after_run_returns_checkpoint_metadata(api: ApiHarness) -> None:
    """run 之后会话可见，且返回最新 checkpoint 与挂起节点。"""
    thread_id = await api.create_thread()
    started = await api.client.post(
        f"{API_PREFIX}/threads/{thread_id}/runs",
        headers=api.auth_headers,
        json={"agent": api.stack.agent_name, "message": "新能源电价机制如何确定？"},
    )
    assert started.status_code == 200, started.text

    response = await api.client.get(f"{API_PREFIX}/threads/{thread_id}", headers=api.auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["thread_id"] == thread_id
    assert body["latest_checkpoint_id"]
    assert body["updated_at"]
    assert body["next"] == []  # 已跑到 END
    assert body["has_interrupt"] is False


async def test_unknown_thread_is_404_not_empty_body(api: ApiHarness) -> None:
    """不存在的会话返回 404 + Problem Details，而不是 200 空对象。"""
    response = await api.client.get(
        f"{API_PREFIX}/threads/demo:demo_user:does-not-exist", headers=api.auth_headers
    )

    assert response.status_code == 404


async def test_missing_credentials_is_401(api: ApiHarness) -> None:
    """无凭据一律 401，且响应体不得提示"token 是什么"（CLAUDE.md 8.2 / 10.1）。"""
    response = await api.client.post(f"{API_PREFIX}/threads")

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
    assert "test-bearer-token" not in response.text


async def test_wrong_token_is_401(api: ApiHarness) -> None:
    """错误 Token 与缺失凭据同响应，不区分提示。"""
    response = await api.client.post(
        f"{API_PREFIX}/threads", headers={"Authorization": "Bearer wrong-token"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


async def test_non_bearer_scheme_is_401(api: ApiHarness) -> None:
    """非 Bearer 方案（如 Basic）同样 401。"""
    token = api.stack.settings.agent_api_bearer_token.get_secret_value()
    response = await api.client.post(
        f"{API_PREFIX}/threads", headers={"Authorization": f"Basic {token}"}
    )

    assert response.status_code == 401


async def test_cross_tenant_thread_id_is_404_not_403(api: ApiHarness) -> None:
    """跨租户访问返回 404：403 会变成"该会话存在"的存在性预言机（CLAUDE.md 10.2 / NO-19）。"""
    other_tenant = compose_thread_id("other_tenant", "someone", "abc123")

    response = await api.client.get(
        f"{API_PREFIX}/threads/{other_tenant}", headers=api.auth_headers
    )

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


async def test_same_tenant_other_user_is_404(api: ApiHarness) -> None:
    """同租户、不同用户同样是 404——会话归属校验到 user 粒度。"""
    other_user = compose_thread_id(api.stack.settings.demo_tenant_id, "another_user", "abc123")

    response = await api.client.get(f"{API_PREFIX}/threads/{other_user}", headers=api.auth_headers)

    assert response.status_code == 404


async def test_malformed_thread_id_is_404(api: ApiHarness) -> None:
    """格式非法的 thread_id 也是 404（与"不存在"不可区分）。"""
    for bad in ("not-a-thread-id", "demo:demo_user", "demo::abc", "a:b:c:d"):
        response = await api.client.get(f"{API_PREFIX}/threads/{bad}", headers=api.auth_headers)
        assert response.status_code == 404, bad
