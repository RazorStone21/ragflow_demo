"""评估执行入口：跑黄金集、录产物、对比基线（CLAUDE.md 9.2 / NO-16）。

把"质量"变成一条可重跑的命令，这样 Prompt / 模型 / 检索策略的改动才有依据——
在此之前，NO-16 要求"改这些必须先跑评估回归"，但没有任何东西能让这句话被执行。

用法::

    # 零基础设施：只装载黄金集与门禁阈值，打印覆盖度报告后退出
    uv run python scripts/eval_run.py --validate-only

    # 复核某题的**实际检索证据**（评审人据此翻原文，见 tests/evals/README.md）
    uv run python scripts/eval_run.py --show typical-coal-capacity-price-fixed-cost-ratio

    # 从已录制产物离线复核（无需基础设施）
    uv run python scripts/eval_run.py --show <id> --from local_data/evals/runs/<id>.json

    # 跑检索层（最便宜，不调模型）；先小样本确认链路通
    uv run python scripts/eval_run.py --layer retrieval --limit 3

    # 全量 + 裁判，并与基线对比（跌破阈值时退出码 3）
    uv run python scripts/eval_run.py --layer all --baseline tests/evals/baselines/<file>.json

**为什么 `--validate-only` 必须能完全离线跑**：它是"评审黄金集"这一步的前置检查，
而评审人要能在没有 Docker、没有网关的机器上检查自己那一版黄金集是否合法。

**为什么把 `--show` 做成一等命令**：§9.2 的"不得自评自满"要求评审人对照原文核对。
如果让评审人自己去翻 Milvus 才能看到"这题实际检索到了什么"，这条协议就没人会执行。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Coroutine
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agent_platform.api.container import open_production_deps
from agent_platform.core.errors import (
    AgentPlatformError,
    ArtifactIncompatibleError,
    ConfigurationError,
)
from agent_platform.core.logging import configure_logging, enable_utf8_console, get_logger
from agent_platform.core.models import build_chat_model
from agent_platform.evaluation.judge import CitationJudge
from agent_platform.evaluation.loader import GoldenSet, coverage_report, load_golden_set
from agent_platform.evaluation.ragas_backend import build_ragas_backend
from agent_platform.evaluation.ragas_judge import RagasJudge
from agent_platform.evaluation.reporting import (
    MONITOR_ONLY_METRICS,
    Comparison,
    Gate,
    Thresholds,
    aggregate,
    compare,
    decide_verdict,
    detect_judge_traceability_gap,
    evaluate_gate,
    load_thresholds,
    promote_baseline,
    read_artifact,
    render_case_detail,
    render_comparison,
    render_gate,
    write_artifact,
)
from agent_platform.evaluation.runner import (
    RunnerDeps,
    new_eval_run_id,
    run_case,
    run_golden_set,
)
from agent_platform.evaluation.schemas import (
    STRIPPED_EVIDENCE_CONTENT,
    EvalArtifact,
    EvalLayer,
    GoldenCase,
    JudgeSnapshot,
    RunVerdict,
)
from agent_platform.settings import Settings, load_settings

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_USAGE = 1
"""参数或配置错误：命令行写错、黄金集非法、阈值文件非法。"""
EXIT_INFRA = 2
"""基础设施失败：连不上 Postgres/Milvus/网关，或脚本自身抛异常。"""
EXIT_REGRESSION = 3
"""跌破阈值或出现新失败样例（观察期下只报告，不阻断提交）。"""
EXIT_BASELINE_INCOMPATIBLE = 4
"""基线不可比：产物结构版本不符，或配置快照发生漂移（见 ``Comparison.drifts``）。"""

_LAYER_CHOICES: dict[str, EvalLayer] = {
    "retrieval": EvalLayer.RETRIEVAL,
    "graph": EvalLayer.GRAPH,
    # ``judge`` 与 ``all`` 都跑"图 + 裁判"：judge 天然蕴含 graph（没有答案就无从裁判）。
    # 两个名字都保留是为了让读者不必从"judge 会跑什么"反推它是否包含生成步骤。
    "judge": EvalLayer.JUDGE,
    "all": EvalLayer.JUDGE,
}

# 只有这一层会跑裁判。``graph`` **不**顺便判分：那既与 help 不符，也让"只想看生成结果"
# 的调用悄悄付出 RAGAS 的全部模型与嵌入成本。
_JUDGE_LAYER = EvalLayer.JUDGE


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="eval_run.py",
        description="跑黄金集评估并产出留痕产物（观察期：不阻断提交，只报告）",
        epilog=(
            "退出码：0 达标；1 参数/配置错误；2 基础设施失败；"
            "3 跌破阈值或出现新失败；4 基线不可比。"
        ),
    )
    parser.add_argument(
        "--layer",
        choices=sorted(_LAYER_CHOICES),
        default="all",
        help="执行层次（默认 all）：retrieval 只检索不调模型；graph 只做检索+生成"
        "（不跑裁判，产物记 incomplete）；judge/all 在 graph 之上加 RAGAS 与引用裁判",
    )
    parser.add_argument("--filter", default=None, help="只跑 id 或类别包含该子串的样例")
    parser.add_argument(
        "--limit", type=int, default=None, help="只跑前 N 条（截断会记入产物 notes）"
    )
    parser.add_argument(
        "--out", default=None, help="产物路径（默认 local_data/evals/runs/<run_id>.json）"
    )
    parser.add_argument("--baseline", default=None, help="基线产物路径；给定则做对比")
    parser.add_argument(
        "--promote-baseline",
        default=None,
        metavar="PATH",
        help="把本轮产物提升为入库基线（精简副本，清空正文与流水）；只接受全绿无错的轮次",
    )
    parser.add_argument("--tenant", default=None, help="检索所用租户（默认 DEMO_TENANT_ID）")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="只校验黄金集与阈值并打印覆盖度报告，**完全离线**（不连任何服务）",
    )
    parser.add_argument(
        "--require-reviewed",
        action="store_true",
        help="存在未签署草稿即判失败（退出码 1）——门禁只认已按原文核对过的样例",
    )
    parser.add_argument(
        "--require-relevance-labels",
        action="store_true",
        help="可答题缺少已签署的 relevant_documents 即判失败（退出码 1）；"
        "缺标签时检索三项只会记 N/A，不能进入“样本不足”的软路径",
    )
    parser.add_argument(
        "--no-judge-cache",
        action="store_true",
        help="绕过判分缓存（RAGAS 与引用裁判都不读不写），用于核对缓存是否污染了本轮分数",
    )
    parser.add_argument(
        "--promote-by",
        default="",
        metavar="HUMAN",
        help="提升基线的人（必填于 --promote-baseline）；留痕用，空值会被拒绝",
    )
    parser.add_argument("--show", default=None, metavar="CASE_ID", help="打印该题的证据与指标明细")
    parser.add_argument(
        "--from",
        dest="from_artifact",
        default=None,
        metavar="PATH",
        help="从已录制产物离线读取，不连任何服务；传 latest 取 runs 目录里最新的一份。"
        "配合 --show 看单题明细，配合 --promote-baseline 提升基线",
    )
    parser.add_argument(
        "--skip-judge", action="store_true", help="跳过裁判层（省成本）；产物记为不完整"
    )
    parser.add_argument(
        "--rejudge",
        action="store_true",
        help="与 --from 同用：对既有产物**重跑裁判层**（命中判分缓存则近乎免费），"
        "用于给缺 models.judge 的旧产物补记裁判模型，或换裁判提示词后重判旧答案",
    )
    parser.add_argument("--golden-dir", default=None, help="黄金集目录（默认 EVAL_GOLDEN_DIR）")
    parser.add_argument(
        "--thresholds", default=None, help="门禁阈值文件（默认 EVAL_THRESHOLDS_PATH）"
    )
    return parser


def _select(golden: GoldenSet, needle: str | None) -> list[GoldenCase]:
    """按 ``--filter`` 子串筛样例（匹配 id 或类别）。"""
    if not needle:
        return list(golden.cases)
    return [case for case in golden.cases if needle in case.id or needle in case.category.value]


def _resolve_golden(args: argparse.Namespace, settings: Settings) -> GoldenSet:
    """装载黄金集（路径优先取命令行，其次取配置）。"""
    directory = Path(args.golden_dir) if args.golden_dir else settings.eval_golden_dir
    return load_golden_set(directory)


def _check_reviewed(golden: GoldenSet, *, required: bool) -> int:
    """按 ``--require-reviewed`` 检查签署状态，打印覆盖度报告。"""
    report = coverage_report(golden)
    # 先写报告再判失败：报告是 stdout（带缓冲），失败信息是 stderr（无缓冲），
    # 不 flush 的话 stderr 会插到报告前面，读者看到的是"先报错、后给上下文"。
    sys.stdout.write(report.render() + "\n")
    sys.stdout.flush()
    drafts = golden.drafts
    if not required:
        if drafts:
            sys.stdout.write(
                f"\n提示：{len(drafts)} 条为草稿，未计入正式门禁。"
                f"评审签署后加 --require-reviewed 复核。\n"
            )
        return EXIT_OK
    if drafts:
        sys.stderr.write(f"\n失败：--require-reviewed 下存在 {len(drafts)} 条未签署样例：\n")
        for case in drafts[:10]:
            sys.stderr.write(f"  - {case.id}（reviewed_by 为空）\n")
        if len(drafts) > 10:
            sys.stderr.write(f"  ...另有 {len(drafts) - 10} 条\n")
        sys.stderr.write(
            "\n按 tests/evals/README.md 的评审协议逐条对照原文核对后填写 "
            "reviewed_by / reviewed_at 并置 draft=false。\n"
        )
        return EXIT_USAGE
    return EXIT_OK


def _check_relevance_labels(golden: GoldenSet, *, required: bool) -> int:
    """按 ``--require-relevance-labels`` 检查可答题的标签完整性。

    检索三项只在有标签时采集；缺标签会记 N/A 并让聚合层报"样本不足"。
    那个路径对单测友好，但对正式评估太软——它允许一份**没标完**的黄金集跑完
    并产出一份看起来完整、实际上检索分数缺席的报告。这个开关把缺标签变成
    启动即失败，避免在错误的前提下花掉整轮模型调用。

    Returns:
        ``EXIT_OK`` 或 ``EXIT_USAGE``。
    """
    if not required:
        return EXIT_OK
    missing = [
        case.id for case in golden.cases if case.is_answerable and not case.has_relevance_labels
    ]
    if not missing:
        return EXIT_OK
    sys.stderr.write(
        f"\n失败：--require-relevance-labels 下存在 {len(missing)} 条可答题缺少"
        "已签署的 relevant_documents：\n"
    )
    for case_id in missing[:10]:
        sys.stderr.write(f"  - {case_id}\n")
    if len(missing) > 10:
        sys.stderr.write(f"  ...另有 {len(missing) - 10} 条\n")
    sys.stderr.write(
        "\n先跑 scripts/eval_label_worksheet.py 生成工作表，回原文核对后填写 "
        "relevant_documents / relevance_reviewed_by / relevance_reviewed_at。\n"
    )
    return EXIT_USAGE


def _print_thresholds(thresholds: Thresholds, path: Path) -> None:
    """打印实际生效的阈值来源与取值。

    门禁配置可以经 ``--thresholds`` 换文件，所以"这次到底用了哪个文件、哪些值"
    必须出现在输出里——否则调松门禁的唯一痕迹就是一份没人看过的文件。
    """
    sys.stdout.write(f"\n门禁阈值（{path}）：\n")
    for name, floor in thresholds.thresholds.items():
        minimum = thresholds.min_samples.get(name)
        sample_text = f"，最少样本 {minimum}" if minimum is not None else ""
        sys.stdout.write(f"  {name.value:<26}{floor:.2f}{sample_text}\n")


def _show_from_artifact(artifact: EvalArtifact, case_id: str) -> int:
    """从已录制产物打印某题明细（离线）。"""
    case = next((item for item in artifact.cases if item.case_id == case_id), None)
    if case is None:
        sys.stderr.write(f"产物中没有样例 {case_id}\n")
        return EXIT_USAGE
    sys.stdout.write(render_case_detail(case, question=case.question) + "\n")
    return EXIT_OK


async def _show_live(deps: RunnerDeps, case: GoldenCase, *, run_id: str, tenant_id: str) -> int:
    """跑一次该题的检索层并打印证据（评审人据此翻原文）。"""
    result, foreign = await run_case(
        deps, case, layer=EvalLayer.RETRIEVAL, run_id=run_id, tenant_id=tenant_id
    )
    sys.stdout.write(render_case_detail(result, question=case.question) + "\n")
    if foreign:
        sys.stdout.write(f"\n⚠️ 陌生租户（{tenant_id} 之外）也检索到了 {len(foreign)} 条证据：\n")
        for item in foreign[:5]:
            sys.stdout.write(f"  - {item.document_name}\n")
    return EXIT_OK if not result.error else EXIT_INFRA


def _write_out(artifact: EvalArtifact, args: argparse.Namespace, settings: Settings) -> Path:
    """落盘产物。"""
    target = Path(args.out) if args.out else settings.eval_runs_dir / f"{artifact.run.run_id}.json"
    write_artifact(artifact, target)
    return target


def _resolve_source(args: argparse.Namespace, settings: Settings) -> Path | None:
    """把 ``--from`` 的取值解析成产物路径；``latest`` 取 runs 目录里最新的一份。

    **为什么要 ``latest``**：runs 目录的文件名是带时间戳的 ``run_id``，跑完一轮后
    要复用那一轮只能靠事后去翻目录名。评估的开销在模型调用上，产物已经是既成事实，
    "再看一眼刚才那轮"不该要求人先去把文件名找出来。

    "最新"按 **mtime** 而非文件名排序：文件名里的时间戳是**运行开始**时刻，而
    ``--out`` 落到别处、手工搬移过的产物会让文件名与修改时间不一致；人对"最新"
    的直觉跟的是后者。

    Returns:
        产物路径；``latest`` 且在目录里找不到任何产物时返回 ``None``（由调用方
        报错并给出 ``EXIT_USAGE``）。
    """
    raw = args.from_artifact
    if raw != "latest":
        return Path(raw)
    candidates = sorted(
        settings.eval_runs_dir.glob("*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _build_judges(settings: Settings, *, cache_enabled: bool) -> tuple[RagasJudge, CitationJudge]:
    """构造两套裁判（RAGAS 生成指标 + 自研引用准确率）。

    裁判**不得**走应用层 fallback：换裁判型号等于换测量工具，不能由一次网关
    抖动悄悄换成另一个模型来判分（见 core/models.py 的 use_fallback 说明）。
    """
    ragas = RagasJudge(
        build_ragas_backend(settings),
        cache_dir=settings.eval_judge_cache_dir,
        cache_enabled=cache_enabled,
    )
    chat = build_chat_model(settings, logical_name=settings.judge_model, use_fallback=False)
    citation = CitationJudge(
        chat,
        logical_name=settings.judge_model,
        upstream_model=settings.judge_upstream_model,
        cache_dir=settings.eval_judge_cache_dir,
        cache_enabled=cache_enabled,
    )
    return ragas, citation


def _combined_snapshot(citation: JudgeSnapshot, ragas: RagasJudge) -> JudgeSnapshot:
    """把 RAGAS 的测量工具信息并进引用裁判的快照。

    一份产物只有一个 ``JudgeSnapshot``：两套裁判共用同一个网关 ``judge`` 路由，
    拆成两份只会让"谁判的"这个问题出现两个答案。``max_tokens`` 记 8192：两套
    裁判都**显式**传该值（RAGAS 的 ``llm_factory`` 默认只有 1024，请求级参数
    会覆盖网关路由默认，故不能依赖路由）。它是"测量失败"发生频率的诱因，
    必须与分数一并留痕。
    """
    return citation.model_copy(
        update={
            "backend": "ragas+in-house",
            "library_version": ragas.library_version,
            "ragas_version": ragas.library_version,
            "ragas_metrics": list(ragas.metric_names),
            "embedding_model": ragas.embedding_model,
            "telemetry_disabled": ragas.telemetry_disabled,
        }
    )


async def _apply_judge(
    artifact: EvalArtifact,
    settings: Settings,
    golden: GoldenSet,
    *,
    cache_enabled: bool = True,
) -> EvalArtifact:
    """给产物追加 RAGAS 三项与引用准确率（出域 caveat 随快照写入）。"""
    ragas, citation = _build_judges(settings, cache_enabled=cache_enabled)
    by_id = {case.id: case for case in golden.cases}
    judged = []
    try:
        for result in artifact.cases:
            case = by_id.get(result.case_id)
            if case is None or result.error:
                judged.append(result)
                continue
            result = await ragas.score(case, result)
            judged.append(await citation.score(case, result))
    finally:
        await ragas.aclose()

    snapshot = _combined_snapshot(citation.snapshot(), ragas)
    updated = artifact.model_copy(
        update={
            "cases": judged,
            "run": artifact.run.model_copy(
                update={
                    "judge": snapshot,
                    # ``models.judge`` 必须一并补上：跑图时还不知会不会判分，那里留的是
                    # 空串，而 ``detect_drift`` 正是逐字段比对 ``run.models``——不补的话
                    # 产物里会出现"裁判分数 + 裁判模型栏为空"，且首次基线对比会凭空
                    # 报一条 models.judge 漂移。名字取自裁判自己，不在这里重写一遍。
                    "models": artifact.run.models.model_copy(
                        update={"judge": citation.logical_name}
                    ),
                }
            ),
            "notes": [*artifact.notes, snapshot.caveat],
        }
    )
    # 指标变了，聚合与判定必须跟着重算——否则产物里的 aggregate 会与 cases 自相矛盾，
    # 而报告读的是 aggregate，"自相矛盾"就表现成"分数与实际明细对不上"。
    return updated.model_copy(
        update={"aggregate": aggregate(judged), "verdict": decide_verdict(judged)}
    )


def _rejudge_blocker(artifact: EvalArtifact) -> str:
    """判断既有产物能否重判；不能时返回一句原因。

    基线副本的 ``evidence[].content`` 是剥离标记（或空串）。对着它重判，RAGAS 的
    faithfulness 会"核"一段占位文本，引用裁判也看不到证据正文——产出的分数不是
    "重判结果"，是"对着一份脱敏副本编出来的结果"。必须用原始产物。
    """
    stripped = [
        case.case_id
        for case in artifact.cases
        for item in case.evidence
        if item.content == STRIPPED_EVIDENCE_CONTENT or not item.content.strip()
    ]
    if not stripped:
        return ""
    preview = "、".join(stripped[:5]) + ("…" if len(stripped) > 5 else "")
    return (
        f"{len(stripped)} 条证据的正文已被剥离或为空（样例：{preview}）。"
        "这通常是入库基线副本；请改用原始产物（local_data/evals/runs/<run_id>.json）重判"
    )


async def _rejudge(
    artifact: EvalArtifact,
    settings: Settings,
    golden: GoldenSet,
    *,
    cache_enabled: bool = True,
) -> EvalArtifact:
    """对既有产物重跑裁判层，并把"这是一次补记"写进 notes。

    **为什么需要它**：``models.judge`` 曾因 ``_apply_judge`` 漏写而恒为空，已录制的
    产物因此说不清"谁判的"。重建这一栏**不能靠改 JSON**——那是伪造留痕；只能重新
    跑一次裁判，让产物由评审路径自己重新生成。判分缓存键含提示词名、裁判身份与
    渲染后的完整提示词，同类重判通常整轮命中缓存，代价接近零。

    补记会显式留痕：``notes`` 追加一句说明，``started_at`` 保持原值不动——
    改时间戳等于把"补记"伪装成"原轮就判过"，那正是留痕最不该有的性质。
    """
    updated = await _apply_judge(artifact, settings, golden, cache_enabled=cache_enabled)
    return updated.model_copy(
        update={
            "notes": [
                *updated.notes,
                "本条为补记：本轮曾重跑裁判层（--rejudge），以补上原产物缺失的"
                " models.judge；答案与检索结果未改动，started_at 保持原值。",
            ],
        }
    )


@dataclass(frozen=True)
class _CitationAuditSummary:
    """人工抽检摘要（由 ``eval_audit_citations.py --summarize`` 生成）。"""

    run_id: str
    reviewed: int
    agreement_rate: float


def _audit_summary_path(settings: Settings, run_id: str) -> Path:
    """人工抽检摘要的固定位置（与 ``eval_audit_citations.py`` 同一约定）。"""
    return settings.eval_runs_dir.parent / "audits" / f"{run_id}.summary.json"


def _load_audit_summary(
    artifact: EvalArtifact, settings: Settings
) -> tuple[_CitationAuditSummary | None, str]:
    """读取本轮的人工抽检摘要；缺失或不可信时返回原因。

    人工结论不入库（可能夹带证据原文），所以提升基线时只能查本地摘要文件。
    查不到就拒绝——"没人核过引用"这件事不该在基线上看不出来。
    """
    path = _audit_summary_path(settings, artifact.run.run_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, (
            f"未找到人工抽检摘要 {path}。先跑 scripts/eval_audit_citations.py 导出核对表，"
            "人工填写后加 --summarize 生成摘要"
        )
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"人工抽检摘要不可读：{path}（{type(exc).__name__}）"
    if not isinstance(raw, dict):
        return None, f"人工抽检摘要结构不符：{path}"
    run_id = raw.get("run_id")
    reviewed = raw.get("reviewed")
    rate = raw.get("agreement_rate")
    if run_id != artifact.run.run_id:
        return None, f"人工抽检摘要属于另一轮（{run_id!r} ≠ {artifact.run.run_id!r}）"
    if isinstance(reviewed, bool) or not isinstance(reviewed, int) or reviewed < 1:
        return None, f"人工抽检摘要的 reviewed 必须为正整数：{reviewed!r}"
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        return None, f"人工抽检摘要的 agreement_rate 非法：{rate!r}"
    agreement = float(rate)
    if not 0.0 <= agreement <= 1.0:
        return None, f"人工抽检摘要的 agreement_rate 超出 [0,1]：{rate!r}"
    return _CitationAuditSummary(run_id=run_id, reviewed=reviewed, agreement_rate=agreement), ""


def _promote(
    artifact: EvalArtifact,
    gate: Gate,
    target: Path,
    settings: Settings,
    *,
    promote_by: str,
) -> None:
    """把本轮产物提升为入库基线。

    **门槛只卡"测没测成 / 有没有资格当参照"，不卡"分数好不好看"**：

    - 有样例执行失败 → **拒绝**。那一轮连结果都没有；提成基线等于给后续每一次
      对比塞一个无效参照，而且它看起来与有效基线一模一样，没有任何字段能提示。
    - 判定为 ``incomplete``（``--skip-judge`` 或只跑到 graph 层）→ **拒绝**。指标
      没采全，之后对比会把"这条指标上轮压根没采"读成退化或改进。
    - 判定为 ``fail`` → **允许**，但显著提示。首份基线几乎必然是 fail 的：阈值取自
      实测之前的估计，而基线的作用恰恰是让"改进了多少"可被度量。若要求全绿才给
      基线，就会陷入"要先达标才能建参照、而没有参照就说不清改没改好"的死循环——
      这正是 NO-16 要求"先有基线再动检索策略"时要避免的局面。
    - 测量失败、样本不足、草稿、检索标签缺失、脏工作区、无人工抽检、``--promote-by``
      为空 → **拒绝**。这些都不是"分数难看"，而是"参照物本身不可信"：把一份没测全
      或来源不可追溯的产物钉成基线，之后每一轮对比都会继承这个缺陷。

    拒绝之后**不改变退出码**：提升是调用方额外的意愿，失败信息已足够明确，再叠一个
    退出码只会让"评估本身没过"与"没提成基线"混成一团。
    """
    errored = [item for item in artifact.cases if item.error]
    if errored:
        sys.stderr.write(
            f"\n**拒绝**提升为基线：{len(errored)}/{len(artifact.cases)} 条样例执行失败，"
            "这一轮连结果都没有，不能作为参照物（退出码不受影响）。\n"
        )
        return
    if artifact.verdict is RunVerdict.INCOMPLETE:
        sys.stderr.write(
            "\n**拒绝**提升为基线：本轮判定为 incomplete（指标未采全）。以它作参照，"
            "之后会把“上轮没采”读成退化或改进。\n"
        )
        return
    gap = detect_judge_traceability_gap(artifact)
    if gap:
        sys.stderr.write(
            f"\n**拒绝**提升为基线：{gap}。\n"
            "（§9.2 要求裁判模型随分数一并记录。这类缺口在产物里看不出来，只能在这里拦；"
            "修好后重跑一轮即可。）\n"
        )
        return
    if gate.measurement_failures:
        failed = "、".join(item.metric.value for item in gate.measurement_failures)
        sys.stderr.write(
            f"\n**拒绝**提升为基线：{len(gate.measurement_failures)} 项指标存在测量失败"
            f"（{failed}）。失败样例已从聚合中排除，这份产物只代表“测出来的那部分”，"
            "不能当完整参照。\n"
        )
        return
    if gate.insufficient_samples:
        short = "、".join(
            f"{item.metric.value}（{item.samples}/{item.threshold:.0f}）"
            for item in gate.insufficient_samples
        )
        sys.stderr.write(
            f"\n**拒绝**提升为基线：{len(gate.insufficient_samples)} 项指标样本不足"
            f"（{short}）。样本不足的分数不构成证据，提成基线会让之后每轮都拿它当参照。\n"
        )
        return
    golden = artifact.run.golden
    if golden.reviewed_count < golden.case_count:
        sys.stderr.write(
            f"\n**拒绝**提升为基线：黄金集仍有 "
            f"{golden.case_count - golden.reviewed_count} 条未签署草稿。"
            "先用 --require-reviewed 复核并签署。\n"
        )
        return
    if golden.answerable_count < 1 or golden.relevance_labeled_count != golden.answerable_count:
        sys.stderr.write(
            f"\n**拒绝**提升为基线：可答题 {golden.answerable_count} 条，"
            f"已签署相关文档全集的只有 {golden.relevance_labeled_count} 条。"
            "标签不齐时检索三项没有分母，基线不能建立在它上面"
            "（先跑 --require-relevance-labels 定位缺哪些题）。\n"
        )
        return
    if artifact.run.git_dirty:
        sys.stderr.write(
            "\n**拒绝**提升为基线：产物记录的工作区是脏的（git_dirty=true），"
            "代码状态无法追溯。先提交或清理工作区，再重跑一轮。\n"
        )
        return
    audit, audit_problem = _load_audit_summary(artifact, settings)
    if audit is None:
        sys.stderr.write(
            f"\n**拒绝**提升为基线：{audit_problem}。"
            "引用准确率没有人工核对过，就不能作为门禁依据。\n"
        )
        return
    if audit.agreement_rate < 0.90:
        sys.stderr.write(
            f"\n**拒绝**提升为基线：人工与脚本的引用判定一致率仅 "
            f"{audit.agreement_rate:.2f}（{audit.reviewed} 条，要求 ≥ 0.90）。"
            "先修裁判提示词并重跑，再抽检。\n"
        )
        return
    if not promote_by.strip():
        sys.stderr.write(
            "\n**拒绝**提升为基线：--promote-by 为空。基线要有具体的人为它负责，"
            "不能用匿名产物代替。\n"
        )
        return
    write_artifact(promote_baseline(artifact), target)
    sys.stdout.write(
        f"\n基线已提升：{target}（提升人：{promote_by.strip()}；"
        f"引用人工抽检一致率 {audit.agreement_rate:.2f}，{audit.reviewed} 条）\n"
        "（基线副本：已剥离语料正文与裁判原文，保留命中文档/页码；入库请用 "
        "`test(evals): ...` 单独提交，见 tests/evals/README.md 第二节）\n"
    )
    if not gate.ok:
        sys.stdout.write(
            "⚠️ 本轮**未**通过阈值，仍被提升：这是刻意的——它记录的是“当前真实”"
            "状态，后续改动的价值正是相对它来度量。基线含已知不达标项，不表示本轮达标。\n"
        )


def _report(
    artifact: EvalArtifact,
    gate: Gate,
    comparison: Comparison | None,
    out_path: Path,
    settings: Settings,
    *,
    promote_path: Path | None = None,
    promote_by: str = "",
) -> int:
    """打印结论并给出退出码。

    门禁（``gate``）**无论如何都判**：阈值是"这一轮自己够不够格"，与有没有基线无关。
    ``--baseline`` 只追加"和上一轮比变了没"，不是启用门禁的开关。
    """
    sys.stdout.write(f"\n产物：{out_path}\n")
    sys.stdout.write(f"判定：{artifact.verdict.value}；样例 {len(artifact.cases)} 条\n")
    for note in artifact.notes:
        sys.stdout.write(f"⚠️ {note}\n")

    errored = [item for item in artifact.cases if item.error]
    if errored and len(errored) == len(artifact.cases):
        # **全部**样例执行失败：这不是"质量差"，是"没测成"。两者必须分开报——
        # 把基础设施故障渲染成"判定 fail"，会让人去查 Prompt 与检索策略，
        # 而真正要修的是连不通的那个依赖。退出码也必须离开"回归"语义。
        sys.stderr.write(
            f"\n全部 {len(errored)} 条样例执行失败，本轮**没有采集到任何质量证据**。\n"
            f"首条错误：{errored[0].case_id}: {errored[0].error}\n"
        )
        return EXIT_INFRA
    if errored:
        sys.stdout.write(
            f"\n⚠️ {len(errored)}/{len(artifact.cases)} 条样例执行失败"
            f"（未采集指标，已计入“未通过”，聚合分不含它们）：\n"
        )
        for item in errored[:10]:
            sys.stdout.write(f"  - {item.case_id}: {item.error}\n")

    sys.stdout.write("\n聚合分：\n")
    if not artifact.aggregate:
        sys.stdout.write("  （无指标——采样数为 0，见下方门禁表的“样本”列）\n")
    for name, score in sorted(artifact.aggregate.items()):
        tag = "  ← 监控指标，不设门禁" if name in MONITOR_ONLY_METRICS else ""
        sys.stdout.write(f"  {name:<22}{score:.4f}{tag}\n")

    failed = [item.case_id for item in artifact.cases if not item.passed]
    if failed:
        sys.stdout.write(f"\n未通过的样例（{len(failed)} 条）：\n")
        for case_id in failed:
            sys.stdout.write(f"  - {case_id}\n")

    if comparison is not None:
        sys.stdout.write("\n" + render_comparison(comparison))
        if comparison.drifts:
            # 漂移已经打印，这里只说明它对结论的影响，避免读者把两组数字直接相减。
            sys.stdout.write(
                "\n⚠️ 上表存在配置漂移：基线与本次不在同一套配置下，"
                "分数差异**不能**直接归因于某一次改动。\n"
            )
    else:
        sys.stdout.write("\n" + render_gate(gate))

    # 提升放在判定**之后**：先判"这一轮过没过"，再回答"要不要把它提成参照物"。
    # 两者互不影响——提升被拒不改退出码（见 _promote 的 docstring）。
    if promote_path is not None:
        _promote(artifact, gate, promote_path, settings, promote_by=promote_by)

    # 阈值先判：它管的是"这一轮自己够不够格"。基线对比再补一条"有没有新增退化"。
    if not gate.ok:
        return EXIT_REGRESSION
    if comparison is not None and comparison.newly_failing:
        return EXIT_REGRESSION
    return EXIT_OK if artifact.verdict is RunVerdict.PASS else EXIT_REGRESSION


async def _main_async(args: argparse.Namespace, settings: Settings) -> int:
    """按参数分派到各执行路径。"""
    golden = _resolve_golden(args, settings)
    thresholds_path = Path(args.thresholds) if args.thresholds else settings.eval_thresholds_path
    # 阈值只装载一次，且**在任何基础设施连接之前**：文件写坏了应当立刻报错退出，
    # 而不是先花二十分钟跑完图再倒在读文件上（fail-fast，CLAUDE.md 3.5）。
    thresholds = load_thresholds(thresholds_path)

    if args.validate_only:
        code = _check_reviewed(golden, required=args.require_reviewed)
        if code != EXIT_OK:
            return code
        code = _check_relevance_labels(golden, required=args.require_relevance_labels)
        if code != EXIT_OK:
            return code
        _print_thresholds(thresholds, thresholds_path)
        sys.stdout.write("\n校验通过（未连接任何服务）。\n")
        return EXIT_OK

    if args.from_artifact:
        source = _resolve_source(args, settings)
        if source is None:
            sys.stderr.write(
                f"{settings.eval_runs_dir} 里没有任何产物，先跑一轮再用 --from latest\n"
            )
            return EXIT_USAGE
        try:
            artifact = read_artifact(source)
        except ArtifactIncompatibleError as exc:
            sys.stderr.write(f"产物不可读：{exc}\n")
            return EXIT_BASELINE_INCOMPATIBLE
        if args.show:
            return _show_from_artifact(artifact, args.show)
        if args.rejudge:
            blocker = _rejudge_blocker(artifact)
            if blocker:
                sys.stderr.write(f"拒绝重判：{blocker}\n")
                return EXIT_USAGE
            artifact = await _rejudge(
                artifact,
                settings,
                golden,
                cache_enabled=not args.no_judge_cache,
            )
            # 补记写回留痕本身：产物里缺的正是"谁判的"这一栏，而这里就是日后要查它的地方。
            # 判分缓存键含提示词名、裁判身份与渲染后的完整提示词，故重判通常不改分数、只补字段；
            # 若确实换了提示词版本，分数随之而变也正是重建留痕的目的。
            write_artifact(artifact, source)
        if not args.promote_baseline and not args.rejudge:
            sys.stderr.write(
                "--from 需与 --show CASE_ID、--rejudge 或 --promote-baseline 同时使用\n"
            )
            return EXIT_USAGE
        if not args.promote_baseline:
            sys.stdout.write(f"\n已重判并写回：{source}\n")
            return EXIT_OK
        # 复报既有产物 + 提升为基线：**不连任何服务、不重跑图**。首份基线本就该从
        # 已录制的产物提升出来——若只能在"再跑一轮"里提升，那为了出一份基线就要
        # 再付一次完整的模型调用与等待，而不是把已经测过的那轮结果落成参照物。
        return _report(
            artifact,
            evaluate_gate(artifact.cases, thresholds),
            None,
            source,
            settings,
            promote_path=Path(args.promote_baseline),
            promote_by=args.promote_by,
        )

    code = _check_reviewed(golden, required=args.require_reviewed)
    if code != EXIT_OK:
        return code
    code = _check_relevance_labels(golden, required=args.require_relevance_labels)
    if code != EXIT_OK:
        return code

    _print_thresholds(thresholds, thresholds_path)

    selected = _select(golden, args.filter)
    if not selected:
        sys.stderr.write(f"没有样例匹配 --filter {args.filter!r}\n")
        return EXIT_USAGE
    if args.limit is not None and args.limit < 1:
        sys.stderr.write("--limit 必须 >= 1\n")
        return EXIT_USAGE
    # 指纹沿用整个黄金集的：筛出来的子集仍是"同一版黄金集"的一部分，
    # 若此处重算指纹，同一份黄金集跑两次不同 --filter 会得到两个指纹，基线就不可比了。
    filtered = replace(golden, cases=tuple(selected))

    layer = _LAYER_CHOICES[args.layer]
    tenant_id = args.tenant or settings.demo_tenant_id
    run_id = new_eval_run_id()

    async with open_production_deps(settings) as app_deps:
        deps = RunnerDeps(
            settings=settings,
            graph=app_deps.graph,
            retriever=app_deps.retriever,
            milvus_store=app_deps.milvus_store,
        )
        if args.show:
            case = filtered.by_id(args.show)
            if case is None:
                sys.stderr.write(f"黄金集中没有样例 {args.show}\n")
                return EXIT_USAGE
            return await _show_live(deps, case, run_id=run_id, tenant_id=tenant_id)

        sys.stdout.write(
            f"\n开始评估：layer={args.layer}，样例 {len(filtered.cases)} 条，"
            f"租户 {tenant_id}，运行 {run_id}\n"
        )
        artifact = await run_golden_set(
            deps, filtered, layer=layer, run_id=run_id, tenant_id=tenant_id, limit=args.limit
        )

    if layer is _JUDGE_LAYER and not args.skip_judge:
        artifact = await _apply_judge(
            artifact,
            settings,
            filtered,
            cache_enabled=not args.no_judge_cache,
        )
    else:
        reason = "本次 --skip-judge" if args.skip_judge else f"本次只跑到 {args.layer} 层"
        artifact = artifact.model_copy(
            update={
                "notes": [
                    *artifact.notes,
                    f"{reason}，faithfulness / answer_relevancy / answer_correctness / "
                    "citation_accuracy 未采集",
                ],
                # 指标没采全，判定必须是 INCOMPLETE 而不是沿用 decide_verdict 的 pass——
                # 否则一次 --skip-judge 会产出"全绿"的产物，而那什么都没证明。
                "verdict": RunVerdict.INCOMPLETE,
            }
        )

    out_path = _write_out(artifact, args, settings)

    comparison: Comparison | None = None
    if args.baseline:
        try:
            baseline = read_artifact(Path(args.baseline))
        except ArtifactIncompatibleError as exc:
            sys.stderr.write(f"基线不可比：{exc}\n")
            return EXIT_BASELINE_INCOMPATIBLE
        comparison = compare(baseline, artifact, thresholds)

    return _report(
        artifact,
        evaluate_gate(artifact.cases, thresholds),
        comparison,
        out_path,
        settings,
        promote_path=Path(args.promote_baseline) if args.promote_baseline else None,
        promote_by=args.promote_by,
    )


def _run_async(coro: Coroutine[Any, Any, int]) -> int:
    """运行协程；Windows 上必须换成 Selector 事件循环。

    Windows 默认的 ``ProactorEventLoop`` 不被 psycopg 的异步实现支持，连接检查点库时
    直接抛 ``InterfaceError: Psycopg cannot use the 'ProactorEventLoop'``。生产目标是
    Linux（默认就是 Selector），因此这里只对 Windows 生效。

    用 ``asyncio.run`` 而不是自建循环，是为了让事件循环的建立与关闭交给标准库；
    但 ``asyncio.run`` 走的是当前 policy，所以 policy 必须在它**之前**设好。
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(coro)


def main(argv: list[str] | None = None) -> int:
    """脚本入口。"""
    # 必须在 parse_args 之前：--help 由 argparse 直接写 stdout 并 SystemExit；
    # 且中文证据与判定写 stdout 时，Windows 默认 GBK 会抛 UnicodeEncodeError。
    enable_utf8_console()
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        sys.stderr.write(f"配置错误：{exc}\n")
        return EXIT_USAGE
    configure_logging(level=settings.log_level, force=True)
    try:
        return _run_async(_main_async(args, settings))
    except AgentPlatformError as exc:
        sys.stderr.write(f"评估失败：{exc}\n")
        return EXIT_USAGE
    except KeyboardInterrupt:
        sys.stderr.write("已中断\n")
        return EXIT_INFRA
    except OSError as exc:
        sys.stderr.write(f"基础设施失败：{type(exc).__name__}: {exc}\n")
        return EXIT_INFRA


if __name__ == "__main__":
    raise SystemExit(main())
