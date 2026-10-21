"""裁判提示词模板（CLAUDE.md 5.5：提示词一律资产化、命名版本化）。

命名沿用全仓约定 ``<用途>.<角色>.v<主版本>.jinja``：

- ``citation.judge.v1``：逐条判定被引用证据是否支持答案中对应位置的断言。
  faithfulness / relevancy / correctness 已由 RAGAS 承担（ADR-0006），本层只留
  引用准确率——RAGAS 不提供这项，且它依赖本仓"引用编号 → 证据块"的产物结构。

**为什么不复用 ``agents/gov_policy_qa/prompts`` 的 ``render_prompt``**：那个函数的
模板目录由 ``files(__package__)`` 绑死在智能体自己的包里，是刻意为之（智能体的
提示词不该被别处加载）。评估层有自己的一份加载器，两边的资产互不可见——
裁判提示词与产品提示词的演进节奏本来就不同，共享加载器只会制造耦合。

不可信内容（证据正文、模型答案、文档名）一律先经
``guardrails.sanitize.neutralize_untrusted`` 中和再作为**数据**传入
（CLAUDE.md 10.3）：裁判本身也是会被注入的目标，"让裁判去判断证据里有没有注入"
不能成为"裁判可以被注入"的理由。
"""

from __future__ import annotations

from functools import cache, lru_cache
from importlib.resources import files

from jinja2 import Environment, StrictUndefined, Template, TemplateError

from agent_platform.core.errors import AgentPlatformError

CITATION_JUDGE_PROMPT = "citation.judge.v1"
JUDGE_PROMPT_NAMES = (CITATION_JUDGE_PROMPT,)
"""本层全部裁判提示词名；``JudgeSnapshot.prompt_version`` 由它们拼出。"""


@lru_cache(maxsize=1)
def _environment() -> Environment:
    """模板环境（纯文本场景：不转义、保留换行，与智能体侧配置一致）。"""
    return Environment(
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


@cache
def _template(name: str) -> Template:
    """按名称加载模板（进程内缓存）。"""
    resource = files(__package__).joinpath(f"{name}.jinja")
    return _environment().from_string(resource.read_text(encoding="utf-8"))


def render_prompt(name: str, /, **variables: object) -> str:
    """渲染裁判模板。

    Args:
        name: 模板名（不含 ``.jinja``），如 ``CITATION_JUDGE_PROMPT``。
        **variables: 模板变量。

    Returns:
        渲染后的提示词文本。

    Raises:
        AgentPlatformError: 模板不存在、变量缺失或渲染失败。判分资产缺失时
            必须显式失败——静默发出半截提示词会得到一批"看起来正常"的分数。
    """
    try:
        return _template(name).render(**variables)
    except TemplateError as exc:
        raise AgentPlatformError(
            f"裁判提示词渲染失败: {name}（{type(exc).__name__}）",
            detail="评估裁判配置异常，请联系管理员。",
        ) from exc
    except OSError as exc:
        raise AgentPlatformError(
            f"裁判提示词不可读: {name}", detail="评估裁判资产缺失，请联系管理员。"
        ) from exc


__all__: list[str] = [
    "CITATION_JUDGE_PROMPT",
    "JUDGE_PROMPT_NAMES",
    "render_prompt",
]
