"""集成测试的共享工具函数。

为什么不放 ``conftest.py``：pytest 会按自己的模块名把 conftest 导入一次，测试再
``from tests.integration.conftest import ...`` 就会得到**第二个**模块对象（同名文件
两种导入路径）。函数恰好还能用，但夹具与模块级状态会分叉，是那种"现在没事、加两个
夹具后诡异失败"的坑。分开成普通模块，导入路径唯一。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from pymilvus import AsyncMilvusClient, MilvusException

if TYPE_CHECKING:
    from agent_platform.settings import Settings

HINT = """
集成测试未运行。启用步骤：
  1. 复制 .env.example 为 .env 并填好密钥；
  2. docker compose up -d 并等全部服务 healthy；
  3. 在 .env 中设 RUN_INTEGRATION_TESTS=true；
  4. uv run pytest -m integration
"""


async def http_ready(url: str, *, timeout: float = 3.0) -> bool:
    """服务是否有 HTTP 响应（任何状态码都算"在"）。

    探测根路径而非业务端点：目的是判断"进程起来了没"，不是判断"接口对不对"。
    4xx/5xx 反而是服务活着的证据，因此不用 ``raise_for_status``。

    ``trust_env=False`` 与生产客户端保持一致（见 ``knowledge/gateway_client.py``），
    这一条不是可有可无：httpx 默认会读 Windows 注册表里的系统代理，本机开着 Clash 时
    探测请求被转给代理，**对一个根本没在监听的端口也会拿到 502**——于是探测判"服务在"、
    测试不 skip，紧接着真实客户端（本就有 ``trust_env=False``）直连失败，用例变成 ERROR。
    实测同一份代码两次运行给出不同的 skip/ERROR 组合，正是这个不一致造成的：
    探测走了一条与实际调用不同的网络路径，答的就不是同一个问题。
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            await client.get(url)
    except httpx.HTTPError:
        return False
    return True


async def require_http(url: str, *, name: str) -> None:
    """服务不可达时 skip 并给出启动提示。"""
    if not await http_ready(url):
        pytest.skip(f"{name} 不可达（{url}）\n{HINT}")


async def require_milvus(settings: Settings) -> None:
    """Milvus 不可达时 skip，可达则**不**做任何改动。

    探测走一次 ``has_collection`` 而不是 HTTP 探针：Milvus 的 19530 是 gRPC 端口，
    用 HTTP 去 GET 它在服务健康时同样会失败，会得出"永远 skip"的错误结论。
    探测用的集合名带 ``__reachability_probe__`` 前缀，仅为可辨识，不实际创建。
    """
    client = AsyncMilvusClient(
        uri=settings.milvus_uri, token=settings.milvus_token.get_secret_value()
    )
    try:
        await client.has_collection("__reachability_probe__")
    except MilvusException as exc:
        pytest.skip(
            f"Milvus 不可达（{settings.milvus_uri}，{type(exc).__name__}）\n"
            "请先 docker compose up -d milvus"
        )
    finally:
        await client.close()
