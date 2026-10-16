"""评估执行器（B 层）：跑真实检索与图，把结果录成 ``CaseResult``（CLAUDE.md 9.2）。

**本模块不装配依赖，只消费依赖。** 生产的装配路径只有一条（``api/container.py``
的 ``open_production_deps``），由 ``scripts/eval_run.py`` 建立后把 ``graph`` /
``retriever`` / ``milvus_store`` 传进来。之所以不在这里 import 那个函数，是因为
``api/__init__.py`` 明写"本包不得被内层导入"——评估层 import ``api`` 会拧出一条
``evaluation → api`` 的反向边（CLAUDE.md 2.3 的单向依赖）。传对象而不传工厂，
既守住了分层，又保证了"跑的就是 API 服务那条装配路径"。

执行层次（成本递增）：

- ``EvalLayer.RETRIEVAL``：只调 ``retriever.search``，不动生成模型；
- ``EvalLayer.GRAPH``：``astream`` 跑完整图，另收节点进入序列；
- 裁判层不在这里——它在 ``judge.py``，吃本模块录下的产物（产物自带证据与答案）。

**每个样例的失败都不中断整轮**：单例异常记进 ``CaseResult.error`` 并继续，
``reporting.py`` 把它计成失败而非"没有这条"。这是 NO-06 要求的"失败要出声"：
错误被记录、被展示、被计入判定，唯独不被吞掉。
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Durability, StreamMode

from agent_platform.agents.gov_policy_qa.nodes.generate_answer import build_answer_messages
from agent_platform.agents.gov_policy_qa.state import initial_state, message_text
from agent_platform.core.errors import ConfigurationError
from agent_platform.core.ids import compose_thread_id
from agent_platform.core.logging import get_logger
from agent_platform.evaluation.loader import GoldenSet
from agent_platform.evaluation.reporting import aggregate, decide_verdict
from agent_platform.evaluation.schemas import (
    CaseCategory,
    CaseResult,
    CorpusSnapshot,
    EvalArtifact,
    EvalLayer,
    GoldenCase,
    GoldenSnapshot,
    JudgeSnapshot,
    ModelSnapshot,
    RetrievalSnapshot,
    RunMeta,
)
from agent_platform.evaluation.scoring import score_case
from agent_platform.knowledge.milvus_store import MilvusStore
from agent_platform.knowledge.retriever import Retriever
from agent_platform.knowledge.schemas import EvidenceItem
from agent_platform.settings import Settings
from agent_platform.tenants import TENANT_ID_CONFIG_KEY
from agent_platform.utils.time import utc_now_iso

logger = get_logger(__name__)

EVAL_THREAD_USER = "eval"
"""评估线程的 user 段。

``thread_id`` 三段里，租户段必须是真的租户（检索隔离依赖它），用户段与会话段只
服务于"可清理"：会话段统一以 ``eval-`` 开头，清理时按前缀删即可，不会误伤真实会话
（README 给出清理语句）。
"""

EVAL_FOREIGN_TENANT_ID = "eval_foreign"
"""跨租户探针使用的陌生租户标识（NO-19）。

该租户**不拥有任何语料**（演示期只把文档入库到 ``DEMO_TENANT_ID``），因此它的检索
结果必须恒为空。用固定常量而非随机值：随机租户名会让"某次探测恰好命中"不可复现，
而这里的判据本就是"过滤是否真的下推到了数据源"。
"""

_PROMPT_PACKAGES: tuple[str, ...] = (
    "agent_platform.agents.gov_policy_qa.prompts",
    "agent_platform.evaluation.prompts",
)
"""参与指纹计算的提示词包（CLAUDE.md 5.5：提示词是版本化资产）。"""

_STREAM_MODES: list[StreamMode] = ["tasks", "custom"]
"""订阅的流模式，与 ``api/sse.py`` 保持一致。

必须是 **list**：``langgraph/pregel/main.py`` 用 ``isinstance(stream_mode, list)``
判断，传元组会静默退化成裸 payload，模式判别符随之丢失（详见 ``api/sse.py`` 说明）。
只订阅这两种即可——评估要的是"进了哪些节点"，不需要逐 token 增量。
"""

_DURABILITY: Durability = "sync"
"""检查点写入模式，同 ``api/sse.py``：``async`` 不保证 ``astream`` 结束前落库，
紧随其后的 ``aget_state`` 会读到上一轮的终态。"""

_GIT_TIMEOUT_S = 5.0
"""只读 git 命令的超时。本地仓库的 ``rev-parse`` / ``status`` 应在毫秒级返回；
给 5 秒是为极端情况（巨大工作区、慢盘）留余量。超时按"取不到"处理——
元信息缺失不该把整轮评估拖死。"""


@dataclass(frozen=True)
class RunnerDeps:
    """执行器所需的**对象视图**（调用方从 ``open_production_deps`` 取出后组装）。

    刻意不是 ``AppDeps`` 的别名：评估层只声明自己用得到的那几个对象，
    这样 API 容器增删字段不会牵动评估层，也不会诱导评估层去用无关字段。
    """

    settings: Settings
    graph: CompiledStateGraph[Any, Any, Any, Any]
    retriever: Retriever
    milvus_store: MilvusStore


def new_eval_run_id() -> str:
    """生成评估运行 ID：``eval-<UTC 时间戳>-<8 位十六进制>``。

    时间戳在前是为**排序**（产物目录按文件名排序即时间序），随机后缀在后是为
    **唯一**（同一秒内重跑不覆盖）。不用裸 ``uuid4`` 是因为它不可排序。
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = hashlib.sha256(f"{utc_now_iso()}-{datetime.now(UTC).timestamp()}".encode()).hexdigest()
    return f"eval-{stamp}-{suffix[:8]}"


_EVAL_SLUG_MAX_LEN = 24
"""会话段里保留的样例 id 前缀长度。

留一段**可读**前缀是为了人在 Postgres 里一眼认出这是哪个样例；全用摘要更简短，
但排查时就得先查产物才知道 ``eval-9f2c1a…`` 对应哪道题。
"""

_EVAL_DIGEST_LEN = 12
"""``(case_id, run_id)`` 摘要的截取长度（48 位熵，远小于样例规模）。"""


def _eval_session_id(case_id: str, run_id: str) -> str:
    """把 ``(case_id, run_id)`` 压成合法、唯一、长度固定的会话段。

    **为什么必须压缩**：``compose_thread_id`` 对每段限 64 字符（``core/ids.py``），
    而样例 id 最长可达 40 余字符、``run_id`` 另有 29 字符，直接拼接必然越界。实测
    在 ``--layer all`` 上以 ``InvalidThreadIdError`` 全量失败——每条样例都记成"执行
    错误"、聚合为空。这类"越界"不会随样例变多而自愈，所以取定长形式，与输入长度无关。

    唯一性靠摘要而非截断后的名字：``boundary-guizhou-…`` 这类同前缀样例在截断后
    会互相撞车，而两条样例共用一条 thread 意味着后跑的会读到前一条的检查点——
    这是**静默串味**，比直接报错难查得多。摘要同时绑定 run_id，因此同一轮内不重、
    不同轮次之间也不重（每轮检查点干净），而同一 ``(样例, 轮次)`` 重跑仍落到同一条
    thread 上，重放是幂等的。

    Returns:
        ``"eval-{样例前缀}-{12 位摘要}"``，总长约 42 字符，恒 < 64。
    """
    digest = hashlib.sha256(f"{case_id}\x1f{run_id}".encode()).hexdigest()[:_EVAL_DIGEST_LEN]
    return f"eval-{case_id[:_EVAL_SLUG_MAX_LEN]}-{digest}"


def eval_thread_id(tenant_id: str, case_id: str, run_id: str) -> str:
    """构造评估用的 ``thread_id``（可清理、可追溯）。

    Args:
        tenant_id: 真实租户标识；检索隔离依赖它，不得用假值。
        case_id: 黄金集样例 ID。
        run_id: 本次评估运行 ID。

    Returns:
        ``"{tenant}:eval:{会话段}"``；会话段的 ``eval-`` 前缀是清理时的唯一定位依据
        （README 的清理语句按此前缀匹配），``case_id`` 到 thread 的完整映射记录在
        ``CaseResult.thread_id``（会话段里的样例名是截断的，**不能**用它反查）。
    """
    return compose_thread_id(tenant_id, EVAL_THREAD_USER, _eval_session_id(case_id, run_id))


def render_evidence_message(question: str, evidence: Sequence[EvidenceItem]) -> str:
    """经**生产渲染路径**产出证据块的用户消息文本（提示词卫生断言的取样点）。

    必须调用 ``build_answer_messages`` 而不是在评估层照抄一遍渲染逻辑：照抄的话，
    把 ``neutralize_untrusted`` 从生产路径删掉，这份副本依然"干净"，断言永远绿灯
    ——测的是副本，不是生产（见 ``scoring.score_prompt_hygiene``）。

    Raises:
        ConfigurationError: 末条消息不是用户消息。这与"渲染器换了消息顺序"是同一件事，
            **必须显式失败**：若取样取到系统消息（其中没有证据块），栅栏标记一次都不
            出现，卫生指标会永远通过——那是本指标最危险的失效方式，它长得像绿灯。
    """
    human = build_answer_messages(question, list(evidence))[-1]
    if not isinstance(human, HumanMessage):
        raise ConfigurationError(
            f"证据渲染路径的末条消息不是用户消息: {type(human).__name__}",
            detail="提示词渲染器结构已变，评估取样点失效。",
        )
    return message_text(human)


def prompt_fingerprints() -> dict[str, str]:
    """各提示词资产名 → 内容 sha256 前 16 位（Prompt 漂移的探测依据）。

    只记**指纹不记全文**：提示词是受控资产（CLAUDE.md 10.3），产物要能入仓，
    不宜整段复制。包不存在时跳过而不是报错——``evaluation.prompts`` 在裁判层
    落地前可以缺席，这不是错误状态。
    """
    fingerprints: dict[str, str] = {}
    for package in _PROMPT_PACKAGES:
        try:
            directory = files(package)
        except ModuleNotFoundError:
            continue
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            if entry.name.endswith(".jinja"):
                fingerprints[f"{package.rsplit('.', 1)[-1]}/{entry.name}"] = hashlib.sha256(
                    entry.read_bytes()
                ).hexdigest()[:16]
    return fingerprints


def _git_run_sync(*args: str) -> str | None:
    """执行一条只读 git 命令；非零退出返回 ``None``（不抛异常）。

    **为什么是同步的 + ``asyncio.to_thread``，而不是 ``create_subprocess_exec``**：
    Windows 上 psycopg 异步连接要求 ``SelectorEventLoop``，而该循环**不支持创建子进程**
    （``BaseEventLoop._make_subprocess_transport`` 直接 ``raise NotImplementedError``）。
    两者在 Windows 上互斥，只能让 git 走线程。这里用 ``to_thread`` 显式隔离阻塞调用，
    正是 CLAUDE.md 3.2 允许的形式——阻塞的是被隔离的工作线程，不是事件循环。

    实测教训：这一处曾用 ``create_subprocess_exec``，在 Windows 上跑任何一次真实评估
    都会在 ``build_run_meta`` 阶段以 ``NotImplementedError`` 崩掉。
    """
    try:
        completed = subprocess.run(
            ["git", *args], capture_output=True, check=False, timeout=_GIT_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace")


async def _git_run(*args: str) -> str | None:
    """``_git_run_sync`` 的异步包装（把阻塞调用挪到工作线程）。"""
    return await asyncio.to_thread(_git_run_sync, *args)


async def _git_snapshot() -> tuple[str, bool]:
    """取当前提交与工作区是否脏，返回 ``(sha, dirty)``。

    取不到（非 git 环境、未安装 git、超时）时返回 ``("", False)`` 并记警告：这是
    **元信息缺失**，不该让整轮评估失败，但也绝不能静默——产物里 ``git_sha`` 为空
    本身就是"这次结果无法追到某个提交"的显式信号。
    """
    head = await _git_run("rev-parse", "HEAD")
    if head is None:
        logger.warning(
            "eval.git_unavailable",
            extra={"event": "eval.git_unavailable", "reason": "rev-parse 失败或 git 不可用"},
        )
        return "", False
    status = await _git_run("status", "--porcelain")
    return head.strip(), bool((status or "").strip())


async def _manifest_fingerprint(path: Path) -> str:
    """入库清单的内容指纹；文件不存在返回空串。

    清单记录了每个源文件的路径与内容 sha256（幂等键），因此它的指纹变化等价于
    "语料库换过"——比 ``chunk_count`` 更能说明"这次与基线不可比"。

    读文件走 ``asyncio.to_thread``：同步读盘在 async 路径上属阻塞调用
    （CLAUDE.md 3.2 / NO-05），这里显式隔离到线程池执行。
    """
    if not path.is_file():
        return ""
    return hashlib.sha256(await asyncio.to_thread(path.read_bytes)).hexdigest()


async def build_run_meta(
    deps: RunnerDeps,
    golden: GoldenSet,
    *,
    run_id: str,
    tenant_id: str,
    judge: JudgeSnapshot | None = None,
) -> RunMeta:
    """采集本次运行的完整可比性快照（CLAUDE.md 9.2 留痕）。

    Args:
        deps: 执行器依赖。
        golden: 已装载的黄金集（提供内容指纹与签署计数）。
        run_id: 本次运行 ID。
        tenant_id: 本轮评估实际使用的租户；``chunk_count`` 按它收口（见下）。
        judge: 裁判配置快照；``None`` 表示本轮未启用裁判。

    Returns:
        ``RunMeta``；``git_sha`` 为空即表示"无法追溯到提交"。
    """
    settings = deps.settings
    sha, dirty = await _git_snapshot()
    return RunMeta(
        run_id=run_id,
        started_at=utc_now_iso(),
        git_sha=sha,
        git_dirty=dirty,
        prompt_versions=prompt_fingerprints(),
        models=ModelSnapshot(
            chat=settings.primary_chat_model,
            embedding=settings.embedding_model,
            rerank=settings.rerank_model,
            judge=settings.judge_model if judge is not None else "",
        ),
        retrieval=RetrievalSnapshot(
            top_k=settings.evidence_top_k,
            candidates=settings.retrieval_candidates,
            similarity_threshold=settings.similarity_threshold,
            rerank_score_threshold=settings.rerank_score_threshold,
            vector_weight=settings.hybrid_vector_weight,
            dim=settings.milvus_dim,
        ),
        corpus=CorpusSnapshot(
            collection=settings.milvus_collection,
            # 按本轮租户收口（D10，2026-10-17）。原先不带租户，数的是整个集合——
            # 而检索路径全程带租户过滤（NO-19），别的租户的行**根本检索不到**。
            # 后果不是数字变大一点，而是这个字段会因与被评估语料无关的原因漂移：
            # 实测集成测试的夹具残留（`test-tenant-a/b` 各 3 条）把 195 顶成 201，
            # 一份"语料没变"的基线对比因此会喊出语料漂移。快照的意义就是让
            # "变了"只对应真变了，所以这里必须与检索同口径。
            chunk_count=await deps.milvus_store.count(tenant_id=tenant_id),
            manifest_sha256=await _manifest_fingerprint(settings.ingest_manifest_path),
        ),
        judge=judge if judge is not None else JudgeSnapshot(),
        golden=GoldenSnapshot(
            sha256=golden.sha256,
            case_count=len(golden.cases),
            reviewed_count=len(golden.reviewed),
        ),
    )


async def run_case(
    deps: RunnerDeps,
    case: GoldenCase,
    *,
    layer: EvalLayer,
    run_id: str,
    tenant_id: str,
) -> tuple[CaseResult, list[EvidenceItem]]:
    """执行一条样例，返回 ``(结果, 陌生租户看到的证据)``。

    跨租户探针只对 ``security`` 类执行（``score_tenant_isolation`` 对其余类别返回
    不适用）——探针要花一次真实检索，不该为"反正不计分"的样例付这个成本。

    Args:
        deps: 执行器依赖。
        case: 黄金集样例。
        layer: 本次执行到哪一层。
        run_id: 本次运行 ID。
        tenant_id: 真实租户标识。

    Returns:
        ``(CaseResult, foreign_evidence)``；``foreign_evidence`` 仅在安全类样例上可能
        非空（其余类别恒为空列表，因为探针根本没跑）。

    Notes:
        任何异常都被记进 ``CaseResult.error`` 并返回，而不是上抛——单例失败不该中断
        整轮（否则一条坏样例会让其余 27 条看不到结果）。错误在报告中计为失败，
        不会被当成"没有这条"。
    """
    started = time.perf_counter()
    foreign: list[EvidenceItem] = []
    try:
        if layer is EvalLayer.RETRIEVAL:
            outcome = await deps.retriever.search(case.question, tenant_id=tenant_id)
            result = CaseResult(
                case_id=case.id,
                category=case.category,
                question=case.question,
                layer=layer,
                evidence=list(outcome.evidence),
                retrieval_meta=outcome.meta,
            )
        else:
            result = await _run_graph(deps, case, layer=layer, run_id=run_id, tenant_id=tenant_id)

        if case.category is CaseCategory.SECURITY:
            probe = await deps.retriever.search(case.question, tenant_id=EVAL_FOREIGN_TENANT_ID)
            foreign = list(probe.evidence)

        scored = score_case(
            case,
            result,
            foreign_evidence=foreign,
            rendered_prompt=render_evidence_message(case.question, result.evidence),
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        return scored.model_copy(update={"latency_ms": elapsed}), foreign
    except Exception as exc:  # 记录后继续：见 Notes，此处是"报出来"而非"吞掉"
        logger.exception(
            "eval.case_failed",
            extra={
                "event": "eval.case_failed",
                "case_id": case.id,
                "layer": layer.value,
                "error": type(exc).__name__,
            },
        )
        return (
            CaseResult(
                case_id=case.id,
                category=case.category,
                question=case.question,
                layer=layer,
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            ),
            foreign,
        )


async def _run_graph(
    deps: RunnerDeps, case: GoldenCase, *, layer: EvalLayer, run_id: str, tenant_id: str
) -> CaseResult:
    """跑一次完整的图（检索 + 生成），收节点序列与终态。"""
    thread_id = eval_thread_id(tenant_id, case.id, run_id)
    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id, TENANT_ID_CONFIG_KEY: tenant_id}
    }
    trace: list[str] = []
    async for chunk in deps.graph.astream(
        initial_state(case.question), config, stream_mode=_STREAM_MODES, durability=_DURABILITY
    ):
        node = _entering_node(chunk)
        if node is not None:
            trace.append(node)

    snapshot = await deps.graph.aget_state(config)
    values: dict[str, Any] = dict(snapshot.values or {})
    return CaseResult(
        case_id=case.id,
        category=case.category,
        question=case.question,
        layer=layer,
        answer=str(values.get("answer") or ""),
        answer_status=str(values.get("answer_status") or ""),
        citations=list(values.get("citations") or []),
        evidence=list(values.get("evidence") or []),
        retrieval_meta=values.get("retrieval_meta"),
        node_trace=trace,
        thread_id=thread_id,
    )


def _entering_node(chunk: Any) -> str | None:
    """从 ``astream`` 的产出里提取"刚进入的节点名"，非进入事件返回 ``None``。

    ``tasks`` 模式在节点**开始前**发出（带 ``triggers``），在节点结束后再发一次
    （不带）——与 ``api/sse.py`` 用同一个判别依据，保证评估看到的进入时序与
    SSE 客户端看到的一致。
    """
    if not (isinstance(chunk, tuple) and len(chunk) == 2 and isinstance(chunk[0], str)):
        return None
    mode, payload = chunk
    if mode != "tasks" or not isinstance(payload, dict) or "triggers" not in payload:
        return None
    name = payload.get("name")
    return name if isinstance(name, str) else None


async def run_golden_set(
    deps: RunnerDeps,
    golden: GoldenSet,
    *,
    layer: EvalLayer,
    run_id: str,
    tenant_id: str,
    limit: int | None = None,
) -> EvalArtifact:
    """跑完黄金集并组装产物（**不含**裁判层，裁判由 ``judge.py`` 追加）。

    逐例**串行**执行（``for`` 而非 ``gather``）：单例要打网关与 Milvus 各若干次，
    并发会把限流与超时的成因搅成一团，而 28 条的规模省不下多少墙钟时间。
    需要提速时应当先解决分段重试，而不是先开并发（CLAUDE.md 3.2 也要求并发
    外部调用必须显式设上限与异常策略）。

    Args:
        deps: 执行器依赖。
        golden: 已装载的黄金集（调用方已按 ``--filter`` / ``--require-reviewed`` 筛过）。
        layer: 执行层次。
        run_id: 本次运行 ID。
        tenant_id: 真实租户标识。
        limit: 只跑前 N 条；``None`` 表示全部。截断会在产物 ``notes`` 里显式登记
            ——"跑了一半却看起来像全跑了"是最容易骗过人的假绿。

    Returns:
        ``EvalArtifact``；``verdict`` 由 ``reporting.decide_verdict`` 给出，
        ``notes`` 记录本次运行的已知限制（截断等）。
    """
    cases = list(golden.cases[:limit] if limit is not None else golden.cases)
    notes: list[str] = []
    if limit is not None and limit < len(golden.cases):
        notes.append(f"本次仅执行前 {limit} 条（黄金集共 {len(golden.cases)} 条），非全量结果")

    meta = await build_run_meta(deps, golden, run_id=run_id, tenant_id=tenant_id)
    results: list[CaseResult] = []
    for index, case in enumerate(cases, start=1):
        result, _ = await run_case(deps, case, layer=layer, run_id=run_id, tenant_id=tenant_id)
        results.append(result)
        logger.info(
            "eval.case_done",
            extra={
                "event": "eval.case_done",
                "case_id": case.id,
                "index": index,
                "total": len(cases),
                "passed": result.passed,
                "error": result.error or None,
            },
        )

    return EvalArtifact(
        run=meta,
        cases=results,
        aggregate=aggregate(results),
        verdict=decide_verdict(results),
        notes=notes,
    )


__all__: list[str] = [
    "EVAL_FOREIGN_TENANT_ID",
    "EVAL_THREAD_USER",
    "RunnerDeps",
    "build_run_meta",
    "eval_thread_id",
    "new_eval_run_id",
    "prompt_fingerprints",
    "render_evidence_message",
    "run_case",
    "run_golden_set",
]
