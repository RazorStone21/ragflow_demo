"""应用装配与中间件测试（CLAUDE.md 2.2 / 3.7 / 8.1 / 8.2 / 9.3）。

锁定三件事：
1. ``main.py`` 导入期零副作用、``create_app`` 只做装配；
2. lifespan 真实地挂载/摘除依赖容器；
3. request_id 贯穿响应头、Problem Details 与**端点内部**的日志上下文。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from starlette.types import Message, Receive, Scope, Send
from tests.conftest import build_api_stack, build_test_app

from agent_platform import __version__
from agent_platform.api.container import AppDeps, ReadinessCheck
from agent_platform.api.middleware.request_context import REQUEST_ID_HEADER
from agent_platform.core.logging import StructuredJsonFormatter
from agent_platform.main import API_PREFIX

if TYPE_CHECKING:
    from tests.conftest import ApiHarness, OpenApi


def test_main_module_has_no_app_instance() -> None:
    """模块级不得存在 app 实例：那会在 import 时读配置（CLAUDE.md 4.8）。"""
    import agent_platform.main as main_module

    assert not hasattr(main_module, "app"), (
        "main 必须保持工厂形态（uvicorn --factory）；模块级 app 会让缺失环境变量变成 ImportError"
    )


def test_create_app_registers_all_documented_routes(api: ApiHarness) -> None:
    """契约路由齐全：探针在根路径，业务在 /api/v1（CLAUDE.md 8.1 / 11.6）。

    断言对象取 OpenAPI 的 paths 而非 ``app.routes``：后者在本版 FastAPI 里被
    ``_IncludedRouter`` 包了一层，子路由要到请求时才展开，遍历不到；
    而 OpenAPI 正是对外发布的契约（CLAUDE.md 8.5）。
    """
    paths = api.app.openapi()["paths"]

    for path, method in (
        ("/healthz", "get"),
        ("/readyz", "get"),
        (f"{API_PREFIX}/version", "get"),
        (f"{API_PREFIX}/threads", "post"),
        (f"{API_PREFIX}/threads/{{thread_id}}", "get"),
        (f"{API_PREFIX}/threads/{{thread_id}}/runs", "post"),
        (f"{API_PREFIX}/threads/{{thread_id}}/runs/stream", "post"),
        (f"{API_PREFIX}/threads/{{thread_id}}/state", "get"),
        (f"{API_PREFIX}/threads/{{thread_id}}/history", "post"),
    ):
        assert path in paths, f"缺少路由 {path}"
        assert method in paths[path], f"路由 {path} 缺少方法 {method}"

    # 探针不得出现在版本前缀下：K8s 的 probe 路径不该随 API 升版本而漂移
    assert not [item for item in paths if item.startswith(f"{API_PREFIX}/health")]


def test_openapi_schema_is_generable_and_complete(api: ApiHarness) -> None:
    """OpenAPI 必须可生成且无错误（CLAUDE.md 8.5：DTO 即契约，不得文档一套代码一套）。"""
    schema = api.app.openapi()

    assert schema["info"]["version"] == __version__
    paths = schema["paths"]
    run_schema = paths[f"{API_PREFIX}/threads/{{thread_id}}/runs"]["post"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert run_schema == {"$ref": "#/components/schemas/RunResponse"}
    assert "RunRequest" in schema["components"]["schemas"]
    assert "HistoryRequest" in schema["components"]["schemas"]
    # 会话创建用 201，与 8.1 的"POST 创建"语义一致
    assert "201" in paths[f"{API_PREFIX}/threads"]["post"]["responses"]


async def test_lifespan_injects_and_removes_deps(api_factory: OpenApi) -> None:
    """启停路径本身也被测：lifespan 进入后容器可用，退出后必须被摘除。"""
    async with api_factory() as harness:
        assert isinstance(harness.app.state.deps, AppDeps)
    assert not hasattr(harness.app.state, "deps")


async def test_request_after_shutdown_is_problem_details_not_traceback(
    fake_settings: Any,
) -> None:
    """容器已摘除时给 Problem Details，且不得泄露数据库/内部拓扑（CLAUDE.md 8.2 / 10.1）。

    关停瞬间到达的请求是一条真实路径（滚动发布时必然发生），因此不靠绕过 lifespan
    来伪造，而是把 lifespan 与 HTTP 客户端**分开**进出：客户端留着，lifespan 先退。
    """
    app = build_test_app(build_api_stack(fake_settings))
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        async with app.router.lifespan_context(app):
            healthy = await client.get("/healthz")
            assert healthy.status_code == 200

        response = await client.get(f"{API_PREFIX}/threads/demo:demo_user:abc")

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "CONFIG_INVALID"
    lowered = response.text.lower()
    for leak in ("traceback", "postgres", "psycopg", "milvus"):
        assert leak not in lowered, f"响应体泄露内部细节: {leak}"


async def test_request_id_is_echoed_when_caller_supplies_valid_one(api: ApiHarness) -> None:
    """调用方自带合法 request_id 时原样回传（CLAUDE.md 8.1）。"""
    response = await api.client.get("/healthz", headers={REQUEST_ID_HEADER: "trace-abc.123"})

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == "trace-abc.123"


async def test_request_id_is_generated_when_absent(api: ApiHarness) -> None:
    """未提供时服务端生成，且每个请求互不相同。"""
    first = await api.client.get("/healthz")
    second = await api.client.get("/healthz")

    generated = first.headers[REQUEST_ID_HEADER]
    assert generated
    assert generated != second.headers[REQUEST_ID_HEADER]


@pytest.mark.parametrize("bad", ["bad id with spaces", "x" * 200, "semi;colon,comma"])
async def test_illegal_request_id_is_replaced_not_echoed(api: ApiHarness, bad: str) -> None:
    """非法 request_id 必须被丢弃并替换，防日志注入与响应头注入。"""
    response = await api.client.get("/healthz", headers={REQUEST_ID_HEADER: bad})

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] != bad


async def test_request_id_reaches_endpoint_log_context(api_factory: OpenApi) -> None:
    """request_id 必须穿过中间件抵达**端点内部**的日志上下文。

    这是对 "纯 ASGI 中间件" 的回归锁：``BaseHTTPMiddleware`` 会把下游扔进独立 anyio
    task，``contextvars`` 跨不过去——端点里打的日志就会丢掉 request_id，全链路关联断裂。
    断言方式刻意选端到端（触发端点内真实告警日志并按渲染后的 JSON 检查），
    而不是去读 logging 模块的私有 contextvar。
    """

    async def _always_fails() -> None:
        raise OSError("storage unreachable")

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(self.format(record))

    handler = _Capture()
    handler.setFormatter(StructuredJsonFormatter())
    logger = logging.getLogger("agent_platform.api.routes.health")
    logger.addHandler(handler)
    try:
        async with api_factory(
            readiness_checks=(ReadinessCheck(name="postgres", probe=_always_fails),)
        ) as harness:
            response = await harness.client.get("/readyz", headers={REQUEST_ID_HEADER: "ctx-probe"})
    finally:
        logger.removeHandler(handler)

    assert response.status_code == 503
    assert response.json()["failed"] == ["postgres"]
    assert records, "端点内未产生任何日志，无法验证上下文传播"
    payloads = [json.loads(line) for line in records]
    failing = [item for item in payloads if item.get("event") == "readyz.failed"]
    assert failing, f"未捕获到 readyz.failed 日志: {records}"
    assert failing[0]["request_id"] == "ctx-probe"
    # 依赖名可见、异常类型可见，但异常文本与连接串一律不出境（CLAUDE.md 8.2 / 10.1）
    assert failing[0]["dependency"] == "postgres"
    assert failing[0]["error"] == "OSError"
    assert "storage unreachable" not in json.dumps(failing, ensure_ascii=False)


async def test_problem_details_echoes_request_id(api: ApiHarness) -> None:
    """错误响应体与响应头携带同一个 request_id，便于用户报障时对账（CLAUDE.md 8.2）。"""
    response = await api.client.get(
        f"{API_PREFIX}/threads/not-a-valid-thread-id",
        headers={**api.auth_headers, REQUEST_ID_HEADER: "err-probe"},
    )

    assert response.status_code == 404
    assert response.headers[REQUEST_ID_HEADER] == "err-probe"
    assert response.json()["request_id"] == "err-probe"


async def test_request_context_middleware_passes_through_non_http_scopes() -> None:
    """非 HTTP scope（lifespan）必须原样透传，否则应用根本起不来。"""
    from agent_platform.api.middleware.request_context import RequestContextMiddleware

    seen: list[str] = []

    async def _app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    async def _receive() -> Message:
        return {"type": "lifespan.startup"}

    async def _send(message: Message) -> None:
        return

    middleware = RequestContextMiddleware(_app)
    await middleware(cast(Scope, {"type": "lifespan"}), _receive, _send)

    assert seen == ["lifespan"]
