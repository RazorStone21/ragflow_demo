#!/usr/bin/env python
"""端到端冒烟：建会话 → SSE 流式提问 → 打印带引用的回答（CLAUDE.md 8.1 / 8.4）。

它验证的是**编排主线的真实路径**，不是打桩：SSE 帧由真实图产出，检索证据来自
Milvus，答案来自网关后的对话模型。因此本脚本能跑通，就说明这条链是通的。

流程::

    POST /api/v1/threads                      → 取 thread_id（租户/用户来自令牌上下文）
    POST /api/v1/threads/{id}/runs/stream     → 消费 SSE 直到 run.completed / run.error

输出分流（有意为之）：**回答写 stdout**，诊断写 stderr。这样 ``demo_chat.py > answer.txt``
拿到的就是干净的答案，而进度与日志不污染它。

答案正文以 ``run.completed`` 的 ``answer`` 为**权威终态**（契约见 ``api/schemas/events.py``）：
``message.delta`` 只是草稿——降级分支根本不发增量，而引用缺失/护栏拦截会在流式推送**之后**
用整段兜底文案替换掉草稿。只打印增量，前者表现为"答了个空"，后者更糟：把已被撤回的
无引用结论当作最终答案展示（NO-12 要挡的正是它）。

用法::

    uv run python scripts/demo_chat.py --question "2025年新能源电价机制的主要内容？"
    # 无关问题用于验证无证据降级分支（NO-12）
    uv run python scripts/demo_chat.py --question "今天天气怎么样？"

退出码::

    0  收到 run.completed
    1  参数或配置错误
    2  HTTP 层失败（建会话或发起运行被拒）
    3  流内 run.error（服务端已给出可理解原因）
    4  连接中断或协议错误（帧无法解析、连接提前关闭）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any

import httpx

from agent_platform.core.errors import ConfigurationError
from agent_platform.core.logging import configure_logging, enable_utf8_console, get_logger
from agent_platform.settings import Settings, load_settings

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_HTTP = 2
EXIT_RUN_ERROR = 3
EXIT_PROTOCOL = 4

DEFAULT_AGENT = "gov_policy_qa"
"""已注册的智能体名（``agents/registry.py`` 与 ``langgraph.json`` 必须一致）。"""


def _out(text: str) -> None:
    """把人类可读内容写到 stdout。

    用 ``sys.stdout.write`` 而非 ``print``：ruff 的 T20 规则禁 ``print``（NO-07 要防的是
    "调试遗留"），本脚本是 CLI 入口，标准输出就是它的对外接口。此处不靠改 lint 配置
    绕过规则，而是显式表达"这是对外输出"这一意图。
    """
    sys.stdout.write(text)
    sys.stdout.flush()


@dataclass
class Frame:
    """一个解析后的 SSE 帧。"""

    event: str
    data: dict[str, Any]


def parse_frame(block: list[str]) -> Frame | None:
    """把 SSE 帧的若干行解析为 ``Frame``；非事件帧（心跳/空帧）返回 ``None``。

    只处理本平台实际使用的两种行：``event:`` 与 ``data:``。心跳是 ``:`` 开头的注释行，
    按 SSE 规范忽略。

    Raises:
        ValueError: ``data`` 不是合法 JSON 对象——协议破损必须显式失败，
            静默跳过会让"流没输出"看起来像"模型没回答"。
    """
    event = ""
    data_lines: list[str] = []
    for line in block:
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
    if not event and not data_lines:
        return None
    if not event:
        raise ValueError(f"SSE 帧缺少 event 行: {block!r}")
    raw = "\n".join(data_lines)
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"SSE 帧 data 不是合法 JSON: {raw[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"SSE 帧 data 不是 JSON 对象: {type(parsed).__name__}")
    return Frame(event=event, data=parsed)


async def create_thread(
    client: httpx.AsyncClient, *, base_url: str, headers: dict[str, str]
) -> str:
    """创建一个会话，返回 ``thread_id``。

    Raises:
        ConfigurationError: 响应缺少 ``thread_id``（服务端契约破损）。
        httpx.HTTPStatusError: 鉴权失败等 HTTP 错误。
    """
    response = await client.post(f"{base_url}/api/v1/threads", headers=headers)
    response.raise_for_status()
    thread_id = response.json().get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ConfigurationError(
            "创建会话的响应缺少 thread_id", detail="服务端返回了非预期的响应结构。"
        )
    return thread_id


def render_citations(
    citations: list[dict[str, Any]],
    *,
    base_url: str | None = None,
    thread_id: str | None = None,
) -> str:
    """把引用列表渲染为可读文本（CLAUDE.md 7.4：答案必须可溯源）。

    编号**必须沿用 ``Citation.index``**（= 提示词里给证据编的号），不能按列表位置重编：
    ``citations`` 只含**被引用过**的条目，位置与编号因此通常不相等。实测一次真实问答
    中模型引用了证据 {1,2,3,4,7,8}，清单恰有 6 条——按位置重编就印成 ``[1]..[6]``，
    于是正文里的 ``[7]``/``[8]`` 在清单中找不到对应项，清单里的 ``[5]``/``[6]`` 又是错的
    编号，"引用清单"存在的意义（可溯源）在人类可读层面直接断掉。缺 ``index`` 字段时
    回退到位置编号（兼容手写的纯 dict 输入）。

    同时给出 ``base_url`` 与 ``thread_id`` 时，为每条带图引用打印取图端点 URL，
    方便人工验收时直接粘进浏览器；两者缺省则只打印 ``image_id``（单测与离线使用）。
    """
    if not citations:
        return "（本次回答未引用任何证据）"
    lines = []
    for position, item in enumerate(citations, start=1):
        index = item.get("index")
        label = index if isinstance(index, int) and index >= 1 else position
        name = item.get("document_name") or "(未知文档)"
        pages = item.get("page_numbers") or []
        page_text = f" 第 {', '.join(str(p) for p in pages)} 页" if pages else ""
        source = item.get("source_uri") or ""
        lines.append(f"  [{label}] {name}{page_text}{f'  ({source})' if source else ''}")
        lines.extend(_render_citation_images(item, base_url=base_url, thread_id=thread_id))
    return "\n".join(lines)


def _render_citation_images(
    citation: dict[str, Any], *, base_url: str | None, thread_id: str | None
) -> list[str]:
    """渲染一条引用附带的图片（地址可构造时给取图 URL，否则只给 ID）。"""
    images = citation.get("images") or []
    chunk_id = citation.get("chunk_id") or ""
    lines = []
    for image in images:
        if not isinstance(image, dict):
            continue
        image_id = image.get("image_id")
        if not isinstance(image_id, str) or not image_id:
            continue
        if base_url and thread_id and chunk_id:
            url = (
                f"{base_url.rstrip('/')}/api/v1/threads/{thread_id}"
                f"/evidence/{chunk_id}/images/{image_id}"
            )
            lines.append(f"      图片：{url}")
        else:
            lines.append(f"      图片：{image_id}")
    return lines


async def run_stream(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    thread_id: str,
    agent: str,
    question: str,
) -> int:
    """发起流式运行并消费事件，返回退出码。"""
    url = f"{base_url}/api/v1/threads/{thread_id}/runs/stream"
    payload = {"agent": agent, "message": question}
    completed: dict[str, Any] | None = None
    block: list[str] = []
    draft: list[str] = []  # 已推送的增量拼接（草稿），用于判断终态是否需要补打

    async with client.stream("POST", url, headers=headers, json=payload) as response:
        if response.status_code >= 400:
            body = (await response.aread()).decode("utf-8", errors="replace")
            sys.stderr.write(f"发起运行失败：HTTP {response.status_code}\n{body}\n")
            return EXIT_HTTP
        async for line in response.aiter_lines():
            # SSE 以空行为帧分隔符：非空行先攒进 block，遇到空行才成帧
            if line != "":
                block.append(line)
                continue
            if not block:
                # 心跳（": ping"）后紧跟空行，此处 block 为空，属正常情况
                continue
            try:
                frame = parse_frame(block)
            except ValueError as exc:
                sys.stderr.write(f"SSE 帧解析失败：{exc}\n")
                return EXIT_PROTOCOL
            finally:
                block = []
            if frame is None:
                continue
            if frame.event == "message.delta":
                draft.append(_delta_of(frame))
            code = _handle_frame(frame)
            if code is not None:
                return code
            if frame.event == "run.completed":
                completed = frame.data.get("payload") or {}

    if completed is None:
        sys.stderr.write("流已结束但未收到 run.completed（连接可能被中断）\n")
        return EXIT_PROTOCOL

    _emit_authoritative_answer(completed, streamed="".join(draft).strip())
    _out("\n")
    citations = completed.get("citations") or []
    rendered = render_citations(citations, base_url=base_url, thread_id=thread_id)
    _out(f"引用来源（{len(citations)} 条）：\n{rendered}\n")
    _out(f"答案状态：{completed.get('answer_status')}\n")
    return EXIT_OK


def _handle_frame(frame: Frame) -> int | None:
    """渲染一帧；返回非 ``None`` 表示应当以该退出码结束。"""
    payload = frame.data.get("payload") or {}
    if frame.event == "run.started":
        sys.stderr.write(f"[run] run_id={frame.data.get('run_id')} agent={payload.get('agent')}\n")
    elif frame.event == "node.entered":
        sys.stderr.write(f"[node] 进入 {payload.get('node')}\n")
    elif frame.event == "retrieval.evidence":
        count = len(payload.get("evidence") or [])
        sys.stderr.write(f"[retrieval] 命中证据 {count} 条\n")
    elif frame.event == "message.delta":
        _out(_delta_of(frame))
    elif frame.event == "run.error":
        sys.stderr.write(
            f"[error] {payload.get('code')}: {payload.get('message')}"
            f"（可重试：{payload.get('retryable')}）\n"
        )
        return EXIT_RUN_ERROR
    return None


def _delta_of(frame: Frame) -> str:
    """取 ``message.delta`` 帧的增量文本。

    负载键名核实自 ``generate_answer._stream_answer``：``data={"delta": delta}``。delta 的
    读取只有这一处，渲染（:func:`_handle_frame`）与草稿累积（:func:`run_stream`）共用，
    避免两处各写一份键名、日后改一处漏一处。
    """
    payload = frame.data.get("payload") or {}
    return str(payload.get("delta") or "")


def _emit_authoritative_answer(completed: dict[str, Any], *, streamed: str) -> None:
    """打印 ``run.completed.answer``——本轮运行的**权威答案**，必要时说明它替代了草稿。

    为什么不能只打印 ``message.delta``：降级分支（``answer_without_evidence``）按设计不发
    任何增量，只打增量则用户看到一片空白；更糟的是 ``citation_missing`` /
    ``guardrail_blocked``——它们**已经流式推送过草稿**，随后用整段兜底文案替换掉它。
    只打增量就是把"因无有效引用而被撤回的结论"当成最终答案展示（NO-12 挡的正是它）。

    草稿已逐字呈现时不再重复，``demo_chat.py > answer.txt`` 因此拿到的仍是干净答案。

    Args:
        completed: ``run.completed`` 的负载。
        streamed: 已写到 stdout 的增量拼接结果。与节点的归一化口径一致地 strip 后比较
            （``generate_answer._stream_answer`` 返回 ``"".join(parts).strip()``）。
    """
    answer = str(completed.get("answer") or "")
    if not answer:
        # 终态无正文属服务端契约异常：如实说明，不让人误以为"模型答了个空"
        sys.stderr.write("[final] run.completed 未携带答案正文（服务端终态异常）\n")
        return
    if answer.strip() == streamed:
        return
    if streamed:
        sys.stderr.write(
            "[final] 流式草稿与权威终态不一致（草稿已被兜底文案替换），以下为权威答案\n"
        )
    _out(f"{answer}\n")


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="demo_chat.py",
        description="端到端 SSE 冒烟：流式问答并打印带引用的回答",
        epilog="退出码：0 成功；1 参数/配置错误；2 HTTP 失败；3 流内错误；4 协议或连接中断。",
    )
    parser.add_argument("--question", required=True, help="提问内容")
    parser.add_argument("--base-url", default=None, help="服务地址（默认取 AGENT_API_HOST/PORT）")
    parser.add_argument(
        "--token", default=None, help="Bearer Token（默认取 AGENT_API_BEARER_TOKEN）"
    )
    parser.add_argument("--agent", default=DEFAULT_AGENT, help=f"智能体名（默认 {DEFAULT_AGENT}）")
    return parser


def _resolve_base_url(settings: Settings, override: str | None) -> str:
    """解析服务地址：显式参数优先，否则由 host/port 拼装。"""
    if override:
        return override.rstrip("/")
    host = "127.0.0.1" if settings.agent_api_host in {"0.0.0.0", "::"} else settings.agent_api_host
    return f"http://{host}:{settings.agent_api_port}"


async def _main_async(args: argparse.Namespace, settings: Settings) -> int:
    """创建会话并消费一次流。"""
    base_url = _resolve_base_url(settings, args.base_url)
    token = args.token or settings.agent_api_bearer_token.get_secret_value()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    # 读超时略大于心跳间隔即可：SSE 有持续心跳，真卡死时才会触发；
    # 断开连接超时防止连不上时长时间干等。
    timeout = httpx.Timeout(
        connect=10.0, read=float(settings.sse_heartbeat_s * 4), write=30.0, pool=10.0
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            thread_id = await create_thread(client, base_url=base_url, headers=headers)
        except httpx.HTTPStatusError as exc:
            sys.stderr.write(f"创建会话失败：HTTP {exc.response.status_code}\n")
            return EXIT_HTTP
        except (httpx.TransportError, ConfigurationError) as exc:
            sys.stderr.write(f"创建会话失败：{exc}\n")
            return EXIT_HTTP
        logger.info("会话已创建", extra={"event": "demo.thread_created", "thread_id": thread_id})
        return await run_stream(
            client,
            base_url=base_url,
            headers=headers,
            thread_id=thread_id,
            agent=args.agent,
            question=args.question,
        )


def main(argv: list[str] | None = None) -> int:
    """脚本入口。"""
    # 必须在 parse_args 之前：--help 由 argparse 直接写 stdout 并 SystemExit；
    # 且中文答案写 stdout 时，Windows 默认 GBK 会抛 UnicodeEncodeError。
    enable_utf8_console()
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        sys.stderr.write(f"配置错误：{exc}\n")
        return EXIT_USAGE
    configure_logging(level=settings.log_level, force=True)
    try:
        return asyncio.run(_main_async(args, settings))
    except (httpx.TransportError, ValueError) as exc:
        sys.stderr.write(f"连接或协议失败：{exc}\n")
        return EXIT_PROTOCOL
    except KeyboardInterrupt:
        sys.stderr.write("已中断\n")
        return EXIT_PROTOCOL


if __name__ == "__main__":
    raise SystemExit(main())
