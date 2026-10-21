"""RAGAS 旁挂判分器：吃录制产物，产出**第二份**分数（ADR-0003 的"形态 2"）。

**为什么是独立脚本、而不是项目里的模块**：ragas 0.4.3 无条件依赖已归档的
``langchain-community``（违反 NO-14）。**仅凭这一条**，ragas 就不得进项目依赖，
故它只作隔离环境里的离线判分器：项目 ``pyproject.toml`` 与 ``uv.lock`` 零改动。

> 早先还担心它会把 ``langchain-core`` 拖回 < 1.0、连带砸掉 langgraph 1.2.x /
> langchain-openai 1.7.x。**实测否掉了这个担心**（2026-10-16）：隔离环境解析出
> ``langchain-core 1.6.11`` + ``langchain-openai 1.7.2``，即 core 留在 1.x。
> 之所以仍走隔离形态，理由只剩 NO-14 那一条——但那一条本身已经足够。

代价是本脚本**不得 import 本仓任何模块**——隔离环境里没有 ``agent_platform``。
所以它自己解析产物 JSON、自己读环境变量。这是 §3.5"环境变量只在 settings.py 读取"
的一处**经登记**的例外（ADR-0003 已记其理由与边界）；变量名与 ``settings.py``
逐字一致，因此不产生第二套配置命名。

用法::

    # 1. 先产出评测产物（--layer all 的产物里才有答案与证据）
    uv run python scripts/eval_run.py --layer all

    # 2. 再在**隔离环境**里判分；项目锁文件不受任何影响
    uv run --no-project -p 3.12 \\
        --with "ragas>=0.4.3" --with "langchain-community<0.4" --with "langchain-openai" \\
        python scripts/eval_ragas.py --artifact local_data/evals/runs/<run_id>.json

    # 便宜的连通性检查：只打印将要判什么，不发任何模型调用
    uv run --no-project -p 3.12 --with "ragas>=0.4.3" --with "langchain-community<0.4" \\
        --with "langchain-openai" python scripts/eval_ragas.py --artifact <path> --dry-run

**旁挂产物不写进门禁**：本脚本输出的是**另一份**文件（``local_data/evals/ragas/``），
不修改评测产物、不参与 ``--baseline`` 对比。同一指标名由两个后端产出时必须是两个
位置，否则"两套判分法是否一致"这件事就再也看不出来了（ADR-0003 第三节）。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_INFRA = 2

_TELEMETRY_ENV = "RAGAS_DO_NOT_TRACK"

CLOUD_JUDGE_CAVEAT = (
    "本旁挂判分器使用网关 judge 路由（自 2026-10-21 起指向硅基流动的 Qwen），"
    "与在线生成模型 primary-chat（DeepSeek）**不同家族**，不再有同源自偏好偏置。"
    "它与 evaluation/judge.py 的独立性只体现在**判分方法**（不同的提示词与分解方式），"
    "**不体现在模型**——两者共用同一 Qwen，两份分数互相印证只能说明"
    "“换一套判分法结论不变”，不能说明分数本身中立。"
    "另：评估数据（问题、证据、答案）会出域到 SiliconFlow，仅限公开演示语料，"
    "生产须换私有化裁判（CLAUDE.md 5.8 / 10.1，ADR-0005）。"
)
"""与 ``evaluation/judge.py`` 的 ``CLOUD_JUDGE_CAVEAT`` 同义。

刻意各写一份而不是共享常量：本脚本不能 import 本仓模块（见模块 docstring），
共享就得引入项目依赖，那正是形态 2 要避免的事。
"""

_TELEMETRY_CAVEAT = (
    "遥测已强制关闭（RAGAS_DO_NOT_TRACK=true）。**这是运行者的责任而非脚本能保证的**："
    "任何直接 import ragas 的代码路径都会在导入时启动其遥测线程，本脚本管不到那些路径。"
)
"""随旁挂产物留痕的遥测说明。

写进产物而不只是写进本文件，是因为"这一轮跑的时候遥测到底关没关"属于§9.1 要求
留痕的运行条件；事后翻代码只能看到"设计上关了"，看不到"那次实际关了"。
"""


class _UsageError(Exception):
    """参数或输入文件的问题（用法错误，不是基础设施故障）。"""


def _force_telemetry_off() -> tuple[bool, str]:
    """在 import ragas **之前**强制关闭其遥测；返回 ``(此前是否已关, 原值表示)``。

    ragas 的 ``_analytics`` 在**导入时**就起守护线程，按固定间隔把事件 POST 到厂商
    端点，负载含一个稳定 UUID（``get_userid()`` 持久化在 appdirs 里）、ragas 版本与
    模型名。它只在 ``RAGAS_DO_NOT_TRACK`` **字面等于** ``"true"`` 时才停
    （``lru_cache`` 缓存 + 小写比较）。实测该端点经本机代理是可达的（HTTP 200），
    所以这不是"理论风险"。

    **这里是"强制"而不是 ``setdefault``**：环境里一个显式的 ``=false`` 会让遥测照常
    外发，而本脚本的契约是"判分不产生数据出域"（§10.1 / NO-15）。相反设置会被覆盖，
    并在旁挂产物里记为 ``telemetry_env_overridden``——覆盖本身要留痕。
    """
    previous = os.environ.get(_TELEMETRY_ENV)
    already_off = previous is not None and previous.lower() == "true"
    os.environ[_TELEMETRY_ENV] = "true"
    return already_off, repr(previous)


def _read_json_object(path: Path) -> dict[str, Any]:
    """读取一个 JSON 对象。

    Raises:
        _UsageError: 文件不存在、不可读、不是合法 JSON，或顶层不是对象。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _UsageError(f"读不到产物文件 {path}（{type(exc).__name__}: {exc}）") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _UsageError(f"产物不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise _UsageError(f"产物顶层应为 JSON 对象，实为 {type(payload).__name__}")
    return payload


def _openai_client(base_url: str, api_key: str) -> Any:
    """构造经 LiteLLM 网关的 ``openai.AsyncOpenAI`` 客户端（**不直连厂商** —— NO-02）。

    ``trust_env=False`` 与生产客户端一致：本机开着 Clash 时 httpx 会读注册表里的
    系统代理，把发往 localhost 的请求也转给代理（``tests/integration/_support.py``
    记过这个实测教训）。

    返回 ``Any`` 而非精确类型：两个 ``_build_*`` 把它交给 ragas 时本就没有可用的
    类型（ragas 不在项目依赖里），这里如实承认边界，而不是编一个类型出来。

    ``http_client`` 局部变量标 ``Any`` 是**必需的、也是如实的**：静态检查眼下拿
    **项目 venv 的 ``openai`` 3.31.0** 去核对，而它把 ``http_client`` 形参标注为
    ``httpx2.AsyncClient``；本脚本却只在**隔离环境**里运行，那里是 ``openai``
    3.3.0 + ``httpx`` 0.28.1（7.0 spike 实测跑通）。同一个形参在两套环境里指向
    两条不同的 httpx 线，静态检查不可能同时满意。写 ``# type: ignore`` 会把这个
    环境差异掩盖成"已处理"；标 ``Any`` 说的是"此处静态检查给不出可信结论"。
    """
    import httpx
    import openai

    http_client: Any = httpx.AsyncClient(
        trust_env=False, timeout=httpx.Timeout(180.0, connect=10.0)
    )
    return openai.AsyncOpenAI(base_url=base_url, api_key=api_key, http_client=http_client)


def _dependency_versions() -> dict[str, str]:
    """记录隔离环境里**实际解析到**的关键依赖版本。

    **为什么必须记**：本形态是 ``uv run --no-project --with ...``，**没有锁文件**
    ——表达式的解每跑一次都可能变（上游发新版、或某个传递依赖改了约束，结果就不同）。
    分数因此可能在"评估对象零改动"的情况下变化。把版本写进产物，才能事后分辨
    这个变化是评估对象的改动、还是判分环境的漂移（§9.2 要求"裁判模型与标准固定
    并记录版本"）。缺失的包记空串，不编造版本号。
    """
    versions: dict[str, str] = {}
    for name in (
        "ragas",
        "langchain-community",
        "langchain-core",
        "langchain-openai",
        "openai",
        "httpx",
    ):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = ""
    return versions


def _build_llm(base_url: str, api_key: str, model: str, temperature: float) -> Any:
    """构造 ragas 使用的异步 LLM（经网关，见 ``_openai_client``）。

    返回类型标 ``Any``：ragas 不在项目依赖里，mypy 解析不到它的类型。这里不假装
    有类型，也不为它引入 ``# type: ignore``（那会顺手掩盖真实错误）。
    """
    from ragas.llms import llm_factory

    client = _openai_client(base_url, api_key)
    return llm_factory(model, provider="openai", client=client, temperature=temperature)


def _build_embeddings(base_url: str, api_key: str, model: str) -> Any:
    """构造 ragas 使用的异步嵌入客户端（``answer_relevancy`` 需要它算余弦相似度）。

    返回 ``Any`` 的理由同 ``_build_llm``；客户端构造见 ``_openai_client``。

    用 ``ragas.embeddings.base.embedding_factory`` 而非 ``ragas.embeddings`` 上的
    同名别名：后者带废弃告警。
    """
    from ragas.embeddings.base import embedding_factory

    client = _openai_client(base_url, api_key)
    return embedding_factory(provider="openai", model=model, client=client)


def _metric_payload(result: Any) -> dict[str, Any]:
    """把 ragas 的 ``MetricResult`` 转成可落盘的字典（保留裁决理由）。"""
    return {
        "value": float(getattr(result, "value", float("nan"))),
        "reason": str(getattr(result, "reason", "") or "")[:2000],
    }


async def _score_case(
    case: dict[str, Any],
    *,
    faithfulness: Any,
    relevancy: Any | None,
) -> dict[str, Any]:
    """判一条样例，返回该样例的记录。

    **失败只记不抛**：本脚本是旁挂判分器，一条样例判分失败不该让整份产物丢失。
    错误被完整写进 ``error`` 字段并在 stderr 出声，随后由退出码反映——这是"报出来"
    而不是"吞掉"（NO-06 针对的是**静默**，``evaluation/runner.py`` 的 ``run_case``
    用的是同一条理由）。不按类型区分捕获：ragas 会把各家 provider 的异常原样抛出，
    而它不在项目依赖里，拿不到稳定的异常基类可供枚举。
    """
    record: dict[str, Any] = {
        "case_id": str(case.get("case_id") or ""),
        "scores": {},
        "skipped": [],
    }
    question = str(case.get("question") or "")
    answer = str(case.get("answer") or "").strip()
    contexts = [str(item.get("content") or "") for item in (case.get("evidence") or [])]
    contexts = [text for text in contexts if text]

    if not answer:
        record["skipped"].append("本题无答案（图未产出答案），无从判分")
        return record

    try:
        if contexts:
            record["scores"]["faithfulness"] = _metric_payload(
                await faithfulness.ascore(
                    user_input=question, response=answer, retrieved_contexts=contexts
                )
            )
        else:
            # 与 judge.py 同判据：无证据就核不了忠实性。这里记"跳过"而不是记 0 分，
            # 否则"没检索到"会被算成"答得不忠实"，两个问题完全不同。
            record["skipped"].append("本题无检索证据：faithfulness 无据可核")
        if relevancy is not None:
            record["scores"]["answer_relevancy"] = _metric_payload(
                await relevancy.ascore(user_input=question, response=answer)
            )
    except Exception as exc:  # 记录后继续：见 docstring，此处是"报出来"而非"吞掉"
        record["error"] = f"{type(exc).__name__}: {exc}"
        sys.stderr.write(f"  ✗ {record['case_id']} 判分失败：{record['error']}\n")
    return record


def _summarize(records: list[dict[str, Any]]) -> tuple[dict[str, float], dict[str, int]]:
    """聚合各指标的均分与样本数（只统计**真的判了分**的样例）。"""
    totals: dict[str, list[float]] = {}
    for record in records:
        for name, payload in record["scores"].items():
            totals.setdefault(name, []).append(payload["value"])
    scores = {name: round(sum(v) / len(v), 4) for name, v in sorted(totals.items())}
    samples = {name: len(v) for name, v in sorted(totals.items())}
    return scores, samples


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="eval_ragas.py",
        description="用隔离环境的 RAGAS 对评测产物做旁挂判分（ADR-0003 形态 2）",
        epilog="退出码：0 成功；1 参数/输入错误；2 全部样例判分失败。",
    )
    parser.add_argument(
        "--artifact", required=True, help="评测产物 JSON（scripts/eval_run.py 产出）"
    )
    parser.add_argument("--out", default=None, help="旁挂产物路径（默认 local_data/evals/ragas/）")
    parser.add_argument(
        "--base-url", default=None, help="LiteLLM 网关地址（默认 $LITELLM_BASE_URL）"
    )
    parser.add_argument(
        "--judge-model", default=None, help="裁判模型逻辑名（默认 $JUDGE_MODEL，再退到 judge）"
    )
    parser.add_argument(
        "--embedding-model",
        default=None,
        help="嵌入模型逻辑名（默认 $EMBEDDING_MODEL，再退到 embedding）",
    )
    parser.add_argument("--filter", default=None, help="只判 case_id 含该子串的样例")
    parser.add_argument("--limit", type=int, default=None, help="只判前 N 条")
    parser.add_argument(
        "--metrics",
        default="faithfulness,answer_relevancy",
        help="要算的指标（逗号分隔）；answer_relevancy 需要嵌入模型",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将要判什么，不发任何模型调用")
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    """装载产物、逐条判分、落盘旁挂结果。"""
    artifact_path = Path(args.artifact)
    artifact = _read_json_object(artifact_path)

    run_meta = artifact.get("run") or {}
    run_id = str(run_meta.get("run_id") or artifact_path.stem)
    cases = list(artifact.get("cases") or [])
    if args.filter:
        cases = [c for c in cases if args.filter in str(c.get("case_id") or "")]
    if args.limit is not None:
        if args.limit < 1:
            raise _UsageError("--limit 必须 >= 1")
        cases = cases[: args.limit]
    if not cases:
        raise _UsageError("筛选后没有可判的样例")

    wanted = {name.strip() for name in args.metrics.split(",") if name.strip()}
    unknown = wanted - {"faithfulness", "answer_relevancy"}
    if unknown:
        raise _UsageError(f"不支持的指标：{sorted(unknown)}")

    # 判分器与在线生成不同家族（judge=云端 Qwen，primary-chat=DeepSeek），但仍共用
    # 同一 Qwen 作为测量工具。优先用产物里记的逻辑名，使旁挂结果的出处与被判的那一轮
    # 对齐，而不是跟着当前 .env 漂；上游型号只作留痕（型号由网关映射决定）。
    judge_model = args.judge_model or os.environ.get("JUDGE_MODEL") or "judge"
    judge_upstream_model = os.environ.get("JUDGE_UPSTREAM_MODEL") or ""
    artifact_judge = str((run_meta.get("models") or {}).get("judge") or "")
    embedding_model = (
        args.embedding_model
        or os.environ.get("EMBEDDING_MODEL")
        or str((run_meta.get("models") or {}).get("embedding") or "")
        or "embedding"
    )
    base_url = args.base_url or os.environ.get("LITELLM_BASE_URL") or "http://localhost:4000"
    api_key = os.environ.get("LITELLM_API_KEY") or ""

    scorable = [c for c in cases if str(c.get("answer") or "").strip()]
    sys.stdout.write(
        f"产物：{artifact_path}\n运行：{run_id}\n"
        f"样例：{len(cases)} 条，其中有答案可判的 {len(scorable)} 条\n"
        f"指标：{sorted(wanted)}　裁判模型逻辑名：{judge_model}"
    )
    if artifact_judge and artifact_judge != judge_model:
        sys.stdout.write(f"（产物记录的裁判为 {artifact_judge}）")
    sys.stdout.write("\n")

    if args.dry_run:
        for case in cases:
            has_answer = bool(str(case.get("answer") or "").strip())
            contexts = len(case.get("evidence") or [])
            sys.stdout.write(
                f"  - {case.get('case_id')}: 答案={'有' if has_answer else '无'}，"
                f"证据 {contexts} 条\n"
            )
        sys.stdout.write("\n--dry-run：未发任何模型调用，未落盘。\n")
        return EXIT_OK

    if not api_key:
        raise _UsageError("缺少 LITELLM_API_KEY（环境变量）；本脚本不回退去读 .env")

    import ragas

    telemetry_already_off, telemetry_previous = _force_telemetry_off()
    sys.stdout.write(
        f"RAGAS {ragas.__version__}　遥测：已强制关闭（原值 {telemetry_previous}）\n\n"
    )

    llm = _build_llm(base_url, api_key, judge_model, 0.0)
    relevancy_metric = None
    if "answer_relevancy" in wanted:
        relevancy_metric = _make_relevancy(llm, base_url, api_key, embedding_model)
    faithfulness_metric = _make_faithfulness(llm) if "faithfulness" in wanted else None
    if faithfulness_metric is None and relevancy_metric is None:
        raise _UsageError("--metrics 里没有任何可算的指标")

    records: list[dict[str, Any]] = []
    for case in cases:
        record = await _score_case(
            case, faithfulness=faithfulness_metric, relevancy=relevancy_metric
        )
        records.append(record)
        mark = "✗" if record.get("error") else ("·" if record["skipped"] else "✓")
        sys.stdout.write(f"  {mark} {record['case_id']}\n")
        sys.stdout.flush()

    scores, samples = _summarize(records)
    failed = [r for r in records if r.get("error")]
    if failed and len(failed) == len(records):
        sys.stderr.write(f"\n全部 {len(failed)} 条样例判分失败，未产出任何分数。\n")
        return EXIT_INFRA

    payload: dict[str, Any] = {
        "schema_version": 1,
        "source_artifact": {
            "path": str(artifact_path).replace("\\", "/"),
            "run_id": run_id,
            "golden_sha256": str((run_meta.get("golden") or {}).get("sha256") or ""),
        },
        "backend": {
            "library": "ragas",
            "library_version": ragas.__version__,
            "integration_form": "isolated-env: uv run --no-project --with ragas",
            "llm_logical_name": judge_model,
            "llm_upstream_model": judge_upstream_model,
            "artifact_judge_logical_name": artifact_judge,
            "embedding_logical_name": embedding_model if relevancy_metric is not None else "",
            "temperature": 0.0,
            "metrics": sorted({name for record in records for name in record["scores"]}),
            "telemetry_env": _TELEMETRY_ENV,
            "telemetry_already_disabled": telemetry_already_off,
            "telemetry_env_previous": telemetry_previous,
            "dependencies": _dependency_versions(),
        },
        "caveats": [CLOUD_JUDGE_CAVEAT, _TELEMETRY_CAVEAT],
        "aggregate": scores,
        "samples": samples,
        "cases": records,
    }
    out_path = Path(args.out) if args.out else Path("local_data/evals/ragas") / f"{run_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )

    sys.stdout.write(f"\n旁挂产物：{out_path}\n聚合分（样本数）：\n")
    for name, value in scores.items():
        sys.stdout.write(f"  {name:<22}{value:.4f}（{samples.get(name, 0)} 条）\n")
    skipped = sum(len(r["skipped"]) for r in records)
    if skipped:
        sys.stdout.write(f"\n跳过 {skipped} 项（多为“本题无答案”或“无证据可核”）。\n")
    if failed:
        sys.stderr.write(f"\n⚠️ {len(failed)}/{len(records)} 条样例判分失败，聚合分不含它们。\n")
    sys.stdout.write(
        "\n⚠️ 本产物**不参与门禁**：它与 evaluation/judge.py 是同指标的两个后端，"
        "分开存放才能看出裁判偏置有多大（ADR-0003）。\n"
    )
    return EXIT_OK


def _make_faithfulness(llm: Any) -> Any:
    """构造 ``Faithfulness`` 指标（``ascore`` 需 user_input / response / retrieved_contexts）。"""
    from ragas.metrics.collections import Faithfulness

    return Faithfulness(llm=llm)


def _make_relevancy(llm: Any, base_url: str, api_key: str, embedding_model: str) -> Any:
    """构造 ``AnswerRelevancy`` 指标（除 LLM 外还需嵌入客户端算余弦相似度）。"""
    from ragas.metrics.collections import AnswerRelevancy

    embeddings = _build_embeddings(base_url, api_key, embedding_model)
    return AnswerRelevancy(llm=llm, embeddings=embeddings)


def main(argv: list[str] | None = None) -> int:
    """脚本入口。"""
    # Windows 默认 GBK 控制台会让中文输出抛 UnicodeEncodeError。这里不能用本仓的
    # enable_utf8_console()——隔离环境里没有 agent_platform（见模块 docstring）。
    if hasattr(sys.stdout, "reconfigure"):
        # 上面那句 hasattr 只收窄了 sys.stdout；sys.stderr 仍是宽类型，故此处需忽略。
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_main_async(args))
    except _UsageError as exc:
        sys.stderr.write(f"用法错误：{exc}\n")
        return EXIT_USAGE
    except KeyboardInterrupt:
        sys.stderr.write("已中断\n")
        return EXIT_INFRA
    except OSError as exc:
        sys.stderr.write(f"基础设施失败：{type(exc).__name__}: {exc}\n")
        return EXIT_INFRA


if __name__ == "__main__":
    raise SystemExit(main())
