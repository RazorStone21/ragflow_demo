"""版本化 Prompt 模板（CLAUDE.md 5.5：模板与代码分离，命名版本化）。

规则：
- 模板一律为 ``<用途>.<角色>.v<主版本>.jinja``，**不得**改名或就地改语义——
  需要调整语义时新增版本文件并同步更新 ``PROMPT_VERSION``，让轨迹里的
  "用了哪版提示词"可回溯（CLAUDE.md 9.1 埋点要求）。
- 渲染使用 ``StrictUndefined``：模板变量缺失即失败，不静默产出半截提示词。
- 证据等不可信内容由调用方先经 ``guardrails.sanitize.neutralize_untrusted``
  处理后再作为**数据**传入；模板不做转义（目标是纯文本提示词，非 HTML）。
"""

from __future__ import annotations

from functools import cache, lru_cache
from importlib.resources import files

from jinja2 import Environment, StrictUndefined, Template, TemplateError

from agent_platform.core.errors import AgentPlatformError

ANSWER_SYSTEM_PROMPT = "answer.system.v2"
ANSWER_HUMAN_PROMPT = "answer.human.v1"
ANSWER_NO_EVIDENCE_MARKER = "【无据】"
"""模型声明"证据完全无法支撑"的前缀标记（v2 起）。

只在回答的**首个非空白字符**处生效：``nodes/no_evidence_marker`` 用流式前缀门
判定，``generate_answer`` 据此把 ``answered`` 改判为 ``insufficient_evidence``。
它是一条**结构化信号**，不参与文案匹配——答案正文里出现同样的字串不算声明。
"""


@lru_cache(maxsize=1)
def _environment() -> Environment:
    """构造模板环境（纯文本场景：不自动转义，保留换行）。"""
    return Environment(
        undefined=StrictUndefined,
        autoescape=False,  # 输出为纯文本提示词，非 HTML，无 XSS 语境
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


@cache
def _template(name: str) -> Template:
    """按名称加载模板（进程内缓存；包内资源，发布后随 wheel 一起分发）。"""
    resource = files(__package__).joinpath(f"{name}.jinja")
    return _environment().from_string(resource.read_text(encoding="utf-8"))


def render_prompt(name: str, /, **variables: object) -> str:
    """渲染模板。

    Args:
        name: 模板名（不含 ``.jinja`` 后缀），如 ``prompts.ANSWER_HUMAN_PROMPT``。
        **variables: 模板变量。

    Returns:
        渲染后的提示词文本。

    Raises:
        AgentPlatformError: 模板不存在、变量缺失或渲染失败（配置/代码错误，
            必须显式失败而非发出残缺提示词）。
    """
    try:
        return _template(name).render(**variables)
    except TemplateError as exc:
        raise AgentPlatformError(
            f"提示词模板渲染失败: {name}（{type(exc).__name__}）",
            detail="智能体提示词配置异常，请联系管理员。",
        ) from exc
    except OSError as exc:
        raise AgentPlatformError(
            f"提示词模板不可读: {name}", detail="智能体提示词资源缺失，请联系管理员。"
        ) from exc


__all__: list[str] = [
    "ANSWER_HUMAN_PROMPT",
    "ANSWER_NO_EVIDENCE_MARKER",
    "ANSWER_SYSTEM_PROMPT",
    "render_prompt",
]
