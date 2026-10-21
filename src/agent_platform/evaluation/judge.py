"""引用裁判（C 层）：逐条判定被引用证据是否支持对应断言（ADR-0006）。

生成侧的 faithfulness / relevancy / correctness 已交给 RAGAS（``ragas_judge.py``）；
本模块只负责**引用准确率**——RAGAS 不提供这项，且它需要"引用编号 → 被引用证据"
的对应关系，只能在本仓的产物结构上判。

**为什么裁判要单独一个逻辑模型名**：裁判与生成必须能各自换型。评测集评分要固定
版本以便跨版本比较，生成侧却要随产品迭代调型；共用一个逻辑名会让"只换生成模型、
不动裁判"做不到，分数因此失去可比性。网关侧的 ``judge`` 路由即为此存在。

**已知偏置与出域，如实登记而不得掩饰**（见 ``CLOUD_JUDGE_CAVEAT``）：网关
``judge`` 路由指向硅基流动的 Qwen，与生成模型 ``primary-chat``（DeepSeek）不同家族，
没有同源自偏好偏置。但 RAGAS 与本裁判**共用同一个 Qwen**，两者互相印证只能说明
"换一套判分法结论不变"，不能说明分数本身中立。评估数据会出域，仅限公开演示语料。

**缓存键必须含裁判身份与库版本**：``model_key`` 覆盖引用裁判的逻辑名与上游型号；
RAGAS 的缓存身份还含 RAGAS 版本与嵌入模型（见 ``ragas_judge.py``）。任何一项变化
都必须让旧判定失效，而不是依赖谁记得去清缓存。

**解析失败必须响**：裁判输出解析不了时不做"默认给个中间分"的处理，而是记
``ERROR`` 并把原始输出完整留痕——那说明"这次没测出来"，与"测出来是 0 分"是两件事，
混在一起会悄悄污染聚合分（ADR-0003 的教训）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_platform.agents.gov_policy_qa.state import message_text
from agent_platform.core.logging import get_logger
from agent_platform.core.models import ChatModel
from agent_platform.evaluation.prompts import CITATION_JUDGE_PROMPT, render_prompt
from agent_platform.evaluation.schemas import (
    CaseResult,
    GoldenCase,
    JudgeSnapshot,
    MetricName,
    MetricResult,
    MetricVerdict,
)
from agent_platform.guardrails.sanitize import neutralize_untrusted
from agent_platform.knowledge.schemas import EvidenceItem

logger = get_logger(__name__)

CLOUD_JUDGE_CAVEAT = (
    "裁判为云端的 Qwen 模型（网关 judge 路由指向 https://api.siliconflow.cn/v1），"
    "与生成模型 primary-chat（DeepSeek）不同家族，不再有同源自偏好偏置；"
    "但 RAGAS 判分与本引用裁判**共用同一个 Qwen**，两份结果互相印证的独立性"
    "只在判分方法，不在模型。评估数据（问题、检索证据、答案、参考答案）会出域到 "
    "SiliconFlow，仅限公开演示语料，客户数据与生产环境不得使用。"
    "裁判是模型判定而非人工金标准，分数仍应按“有偏测量”解读；"
    "生产须换私有化裁判（CLAUDE.md 5.8 / 10.1，ADR-0005 / ADR-0006）。"
)

_REPAIR_INSTRUCTION = (
    "你上一次的输出不是合法的 JSON 对象。请只输出那个 JSON 对象本身，"
    "不要任何解释、前后缀或代码块标记。"
)

_CACHE_SCHEMA_VERSION = 2
"""缓存条目结构版本；判分契约变化时旧缓存自动失效。

v2（2026-10-21）：缓存键加入 ``model_key``（裁判逻辑名 + 上游型号），
换裁判型号不再复用旧判定。RAGAS 条目沿用同一结构，身份串由
``RagasBackend.cache_identity`` 提供（含库版本、嵌入模型与指标配置）。
"""

V = TypeVar("V", bound=BaseModel)


class JudgeParseError(Exception):
    """裁判输出无法解析为约定结构（内部异常，不外泄到 HTTP 边界）。"""


class CitationJudgement(BaseModel):
    """裁判对一条引用的判定。"""

    model_config = ConfigDict(frozen=True)

    index: int = Field(ge=1, description="对应答案中的引用编号")
    supported: bool = Field(description="该引用指向的证据是否支持对应断言")
    reason: str = Field(default="", max_length=1000, description="一句话判定依据")


class CitationVerdict(BaseModel):
    """引用裁判的结构；索引集合由 ``_parse_citations`` 校验。"""

    model_config = ConfigDict(frozen=True)

    citations: list[CitationJudgement] = Field(default_factory=list)


@dataclass(frozen=True)
class _AskResult(Generic[V]):  # noqa: UP046 —— PEP 695 泛型语法需 3.12+，而
    # ``requires-python`` 声明 ``>=3.11,<3.13``；按声明的地板写，不用只在新解释器上
    # 才合法的语法。（ruff 的 ``target-version`` 是 py312，故此处会提示升级。）
    """裁判调用的结果载体。

    ``verdict is None`` 表示两次都没解析出来，**同时**带着 ``raw``——
    失败路径也必须交出原始输出，否则"详见 judge_raw"就成了一句空话。
    """

    verdict: V | None
    raw: str
    cache_hit: bool


def extract_json_object(text: str) -> dict[str, Any]:
    """从模型输出里取出第一个完整的 JSON 对象。

    不用 ``json.loads`` 直接解析全文：裁判模型常会在 JSON 前后加一句
    "好的，以下是结果："或用代码块包裹。用括号配平扫描（并正确处理字符串内的
    引号与转义）比正则可靠——正则数不清嵌套花括号。

    Args:
        text: 模型原始输出。

    Returns:
        解析出的字典。

    Raises:
        JudgeParseError: 找不到配平的 JSON 对象，或顶层不是对象。
    """
    start = text.find("{")
    if start < 0:
        raise JudgeParseError("输出中找不到 JSON 对象")
    depth = 0
    in_string = False
    escaped = False
    for position in range(start, len(text)):
        char = text[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return _load_object(text[start : position + 1])
    raise JudgeParseError("JSON 对象的括号未配平（输出可能被截断）")


def _load_object(fragment: str) -> dict[str, Any]:
    """解析一段 JSON 文本并确认顶层是对象。"""
    try:
        parsed: Any = json.loads(fragment)
    except json.JSONDecodeError as exc:
        raise JudgeParseError(f"JSON 解析失败: {exc.msg}（位置 {exc.pos}）") from exc
    if not isinstance(parsed, dict):
        raise JudgeParseError(f"JSON 顶层不是对象: {type(parsed).__name__}")
    return parsed


def _parse_citations(payload: dict[str, Any], expected: frozenset[int]) -> CitationVerdict:
    """校验引用裁判结构：索引集合必须与产物中的引用**完全一致**。

    缺项、多项、重复都按解析失败处理——那说明裁判没照做，不是"没被引用的那条
    不支持"。把缺失项默认成 false 会凭空造一个失败判定，默认成 true 更糟。
    """
    try:
        verdict = CitationVerdict.model_validate(payload)
    except ValidationError as exc:
        raise JudgeParseError(f"引用裁判结构不符: {exc.error_count()} 处") from exc
    indices = [item.index for item in verdict.citations]
    if len(indices) != len(set(indices)):
        raise JudgeParseError(f"引用裁判返回了重复索引: {sorted(indices)}")
    if set(indices) != set(expected):
        raise JudgeParseError(
            f"引用裁判索引集合 {sorted(set(indices))} 与引用清单 {sorted(expected)} 不一致"
        )
    return verdict


def _cited_blocks(result: CaseResult) -> tuple[list[dict[str, Any]], list[int]]:
    """按 ``chunk_id`` 把引用映射到证据块；返回 ``(块列表, 找不到的引用编号)``。

    正文与文档名一律先中和（CLAUDE.md 10.3）：裁判同样是注入目标，让裁判去判断
    "证据里有没有注入指令"，不能成为"裁判可以被注入"的理由。
    """
    by_chunk = {item.chunk_id: item for item in result.evidence}
    blocks: list[dict[str, Any]] = []
    missing: list[int] = []
    for citation in result.citations:
        target = by_chunk.get(citation.chunk_id)
        if target is None:
            missing.append(citation.index)
            continue
        blocks.append(_block(citation.index, target))
    return blocks, missing


def _block(index: int, item: EvidenceItem) -> dict[str, Any]:
    """一条被引用证据的模板变量。"""
    return {
        "index": index,
        "document_name": neutralize_untrusted(item.document_name),
        "pages": item.page_numbers,
        "content": neutralize_untrusted(item.content),
    }


class JudgeCache:
    """判分缓存：键 = 提示词名 + **裁判身份** + **渲染后完整提示词**的指纹。

    存储位置在 ``local_data/``，不入仓。引用裁判与 RAGAS 共用这一实现：
    前者的键材料是渲染后的提示词，后者是规范化 JSON 请求。

    为什么要缓存：裁判调用是整条评估链上最贵的一步，而"改了一条黄金集题目却要
    重判全部样例"会让人不敢动评测集。

    键为什么是"判定输入"而不是"样例 ID + 答案"：**答案不决定分数**。引用裁判的
    判定输入是（问题 + 答案 + 引用 + 被引用证据），RAGAS 是（问题 + 答案 + 证据
    [+ 参考答案]）；按答案指纹缓存会让输入不同的样例撞键，后者直接拿到前者的分。
    判定输入的超集做键不含歧义；提示词模板改一个字也会命中不了旧缓存。

    ``model_key`` 含裁判身份（逻辑名 @ 上游型号）；RAGAS 还含库版本与嵌入模型。
    同一输入换测量工具后不得复用旧判定——把它并入键材料，比"换型号时记得清缓存"
    可靠。

    命中缓存时显式标记（``JudgeOutcome.cache_hit`` / 指标 detail）——"这次分数是
    真跑出来的还是从缓存里翻出来的"必须能从留痕里读出来。
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @staticmethod
    def key(*, prompt_name: str, model_key: str, prompt: str) -> str:
        """算出缓存键（各段用 ``\\x1f`` 分隔，避免拼接歧义）。"""
        material = "\x1f".join((prompt_name, model_key, prompt))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def path_for(self, key: str) -> Path:
        """缓存文件路径（按前两位分目录，避免单目录堆数千文件）。"""
        return self._directory / key[:2] / f"{key}.json"

    async def load(self, key: str) -> dict[str, Any] | None:
        """读缓存；缺失、损坏或版本不符一律返回 ``None``（缓存失配不是错误）。"""
        path = self.path_for(key)
        try:
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "eval.judge_cache_corrupt",
                extra={"event": "eval.judge_cache_corrupt", "key": key[:12]},
            )
            return None
        if not isinstance(payload, dict) or payload.get("v") != _CACHE_SCHEMA_VERSION:
            return None
        if not isinstance(payload.get("verdict"), dict) or not isinstance(payload.get("raw"), str):
            return None
        return payload

    async def store(self, key: str, *, verdict: dict[str, Any], raw: str) -> None:
        """写缓存；写不进去只记警告（缓存是加速手段，不是正确性依赖）。"""
        body = json.dumps(
            {"v": _CACHE_SCHEMA_VERSION, "verdict": verdict, "raw": raw},
            ensure_ascii=False,
            indent=2,
        )
        try:
            await asyncio.to_thread(_write_text, self.path_for(key), body)
        except OSError as exc:
            logger.warning(
                "eval.judge_cache_write_failed",
                extra={
                    "event": "eval.judge_cache_write_failed",
                    "key": key[:12],
                    "error": type(exc).__name__,
                },
            )


def _write_text(path: Path, body: str) -> None:
    """同步写文件（经 ``asyncio.to_thread`` 调用，见 ``JudgeCache.store``）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class CitationJudge:
    """引用准确性裁判（CLAUDE.md 9.2 / ADR-0006）。"""

    def __init__(
        self,
        model: ChatModel,
        *,
        logical_name: str,
        cache_dir: Path,
        upstream_model: str = "",
        max_tokens: int = 8192,
        cache_enabled: bool = True,
    ) -> None:
        """
        Args:
            model: 经网关构造的裁判模型（``temperature=0``，CLAUDE.md 5.3）。
            logical_name: 网关逻辑模型名（埋点与快照记录用；不得写死型号，NO-03）。
            cache_dir: 判分缓存目录。
            upstream_model: 网关 judge 路由背后的上游型号（仅留痕与缓存键用，
                不参与路由）。型号只来自配置 ``JUDGE_UPSTREAM_MODEL``。
            max_tokens: 单次裁判输出上限。引用裁判按"每条引用一句理由"输出，
                上限给宽不增加正常开销，只把截断风险推远（D8 的教训）。
                与 RAGAS 路径取同一个值（``evaluation/ragas_backend.py``），
                这样 ``JudgeSnapshot.max_tokens`` 能如实代表两套裁判。
            cache_enabled: ``False`` 时完全绕过缓存（``--no-judge-cache``），
                用于核对缓存是否污染了本轮分数。
        """
        self._model = model
        self._logical_name = logical_name
        self._upstream_model = upstream_model
        self._model_key = f"{logical_name}@{upstream_model}"
        self._cache = JudgeCache(cache_dir)
        self._cache_enabled = cache_enabled
        self._max_tokens = max_tokens

    @property
    def logical_name(self) -> str:
        """网关逻辑模型名（写进 ``ModelSnapshot.judge``）。"""
        return self._logical_name

    def snapshot(self) -> JudgeSnapshot:
        """引用裁判的配置快照；RAGAS 字段由调用方合并进同一份快照。"""
        return JudgeSnapshot(
            backend="in-house",
            library_version="",
            temperature=0.0,
            max_tokens=self._max_tokens,
            prompt_version=CITATION_JUDGE_PROMPT,
            upstream_model=self._upstream_model,
            caveat=CLOUD_JUDGE_CAVEAT,
        )

    async def score(self, case: GoldenCase, result: CaseResult) -> CaseResult:
        """对一条样例补上 ``citation_accuracy``，返回追加了指标的新结果。

        Args:
            case: 黄金集样例（提供问题原文）。
            result: 图层的执行结果。

        Returns:
            新的 ``CaseResult``；``judge_raw`` 追加本次裁判的原始输出。
        """
        if not result.citations:
            metric = MetricResult(
                name=MetricName.CITATION_ACCURACY,
                verdict=MetricVerdict.NOT_APPLICABLE,
                detail="本题没有引用，引用准确率不适用（结构红线由 citation_integrity 判）",
            )
            return _with_metrics(result, [metric])

        blocks, missing = _cited_blocks(result)
        if missing:
            metric = MetricResult(
                name=MetricName.CITATION_ACCURACY,
                verdict=MetricVerdict.FAIL,
                score=0.0,
                detail=f"引用 {sorted(missing)} 无法按 chunk_id 对应到任何证据块，无法判定",
            )
            return _with_metrics(result, [metric])

        rendered = render_prompt(
            CITATION_JUDGE_PROMPT,
            question=neutralize_untrusted(case.question),
            answer=neutralize_untrusted(result.answer),
            citations=[
                {
                    "index": citation.index,
                    "document_name": neutralize_untrusted(citation.document_name),
                    "pages": citation.page_numbers,
                }
                for citation in result.citations
            ],
            evidence=blocks,
        )
        expected = frozenset(citation.index for citation in result.citations)
        ask = await self._ask(
            prompt_name=CITATION_JUDGE_PROMPT,
            prompt=rendered,
            parse=lambda payload: _parse_citations(payload, expected),
        )
        if ask.verdict is None:
            return _with_metrics(
                result,
                [
                    MetricResult(
                        name=MetricName.CITATION_ACCURACY,
                        verdict=MetricVerdict.ERROR,
                        detail=(
                            "引用裁判输出两次均无法解析为约定结构：此项**未采集到结果**，"
                            "已从聚合中排除，本样例因此判不通过。原始输出见 judge_raw。"
                        ),
                    )
                ],
                raw=_raw_section(CITATION_JUDGE_PROMPT, ask.raw),
            )

        total = len(ask.verdict.citations)
        supported = sum(1 for item in ask.verdict.citations if item.supported)
        unsupported = [
            f"[{item.index}] {item.reason or '裁判未给出理由'}"
            for item in ask.verdict.citations
            if not item.supported
        ]
        detail = f"{supported}/{total} 条引用的证据支持对应断言"
        if unsupported:
            detail += "；不支持：" + "；".join(unsupported[:3])
        if ask.cache_hit:
            detail += "（缓存命中）"
        metric = _graded_metric(MetricName.CITATION_ACCURACY, supported / total, detail=detail)
        return _with_metrics(result, [metric], raw=_raw_section(CITATION_JUDGE_PROMPT, ask.raw))

    async def _ask(
        self,
        *,
        prompt_name: str,
        prompt: str,
        parse: Callable[[dict[str, Any]], V],
    ) -> _AskResult[V]:
        """带缓存与一次纠偏重试的裁判调用。

        重试只有**一次**：第二次仍失败说明问题不在"偶发的格式抖动"，再试下去
        只是把一次调试事故拖成一条慢链路，并掩盖真正该看的原始输出。
        """
        key = JudgeCache.key(prompt_name=prompt_name, model_key=self._model_key, prompt=prompt)
        if self._cache_enabled:
            cached = await self._cache.load(key)
            if cached is not None:
                try:
                    cached_verdict = parse(cached["verdict"])
                except JudgeParseError:
                    logger.warning(
                        "eval.judge_cache_unusable",
                        extra={"event": "eval.judge_cache_unusable", "prompt": prompt_name},
                    )
                else:
                    return _AskResult(verdict=cached_verdict, raw=cached["raw"], cache_hit=True)

        first = await self._invoke(prompt)
        verdict: V | None = _try_parse(first, parse, prompt_name)
        raw = first
        if verdict is None:
            logger.warning(
                "eval.judge_repair_attempt",
                extra={"event": "eval.judge_repair_attempt", "prompt": prompt_name},
            )
            second = await self._invoke(prompt, repair=True)
            verdict = _try_parse(second, parse, prompt_name)
            raw = f"【首次输出】\n{first}\n\n【纠偏重试输出】\n{second}"
        if verdict is None:
            return _AskResult(verdict=None, raw=raw, cache_hit=False)

        if self._cache_enabled:
            await self._cache.store(key, verdict=verdict.model_dump(mode="json"), raw=raw)
        return _AskResult(verdict=verdict, raw=raw, cache_hit=False)

    async def _invoke(self, prompt: str, *, repair: bool = False) -> str:
        """调用裁判模型并返回纯文本输出（``repair`` 时附上纠偏指令）。"""
        content = prompt if not repair else f"{prompt}\n\n{_REPAIR_INSTRUCTION}"
        message = await self._model.ainvoke(
            [HumanMessage(content=content)], max_tokens=self._max_tokens
        )
        return message_text(message)


def _try_parse(  # noqa: UP047 —— 同 _AskResult：requires-python 声明支持 3.11
    raw: str, parse: Callable[[dict[str, Any]], V], prompt_name: str
) -> V | None:
    """尝试解析一段裁判输出；失败返回 ``None``（不抛，交由调用方决定是否重试）。"""
    try:
        return parse(extract_json_object(raw))
    except JudgeParseError as exc:
        logger.info(
            "eval.judge_parse_failed",
            extra={"event": "eval.judge_parse_failed", "prompt": prompt_name, "reason": str(exc)},
        )
        return None


def _raw_section(prompt_name: str, raw: str) -> str:
    """给原始输出加上提示词名标签（留痕的一段）；空输出返回空串。"""
    return f"[{prompt_name}]\n{raw}" if raw else ""


def _with_metrics(
    result: CaseResult, metrics: Sequence[MetricResult], *, raw: str = ""
) -> CaseResult:
    """把新指标并入结果（替换同名项），并按需追加 ``judge_raw``。"""
    names = {metric.name for metric in metrics}
    preserved = [metric for metric in result.metrics if metric.name not in names]
    judge_raw = result.judge_raw
    if raw:
        judge_raw = f"{judge_raw}\n\n{raw}" if judge_raw else raw
    return result.model_copy(update={"metrics": [*preserved, *metrics], "judge_raw": judge_raw})


def _graded_metric(name: MetricName, score: float, *, detail: str) -> MetricResult:
    """构造分级分：逐例只把"完全没有"判 FAIL，是否达标交给聚合阈值。"""
    verdict = MetricVerdict.PASS if score > 0.0 else MetricVerdict.FAIL
    return MetricResult(name=name, verdict=verdict, score=score, detail=detail)


__all__: list[str] = [
    "CLOUD_JUDGE_CAVEAT",
    "CitationJudge",
    "CitationJudgement",
    "CitationVerdict",
    "JudgeCache",
    "JudgeParseError",
    "extract_json_object",
]
