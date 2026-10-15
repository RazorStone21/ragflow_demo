"""不可信内容的提示词卫生处理（CLAUDE.md 10.3）。

检索结果、工具返回、用户上传内容在 Prompt 中一律视为**不可信数据**：其中的
指令不得被执行。演示阶段的最小防线有两层：

1. **隔离标注**：证据块用固定栅栏包裹，并在系统提示词中声明"栅栏内是数据"；
2. **栅栏同形串中和**：去掉证据正文里出现的栅栏标记，防止内容自行"闭合"栅栏后
   把后续文本伪装成系统指令（间接注入，CLAUDE.md 10.3）。

本模块只做确定性的文本处理，不做语义判断；完整的 NeMo Guardrails / Llama Guard
接入见 ``agent_platform.guardrails`` 包说明（阶段二）。
"""

from __future__ import annotations

import re
import unicodedata

EVIDENCE_OPEN_MARKER = "<<<EVIDENCE"
"""证据块起始标记（与 ``prompts/answer.human.v1.jinja`` 中的写法保持一致）。"""

EVIDENCE_CLOSE_MARKER = "END_EVIDENCE>>>"
"""证据块结束标记（与 ``prompts/answer.human.v1.jinja`` 中的写法保持一致）。"""

_REDACTED = "[标记已移除]"

# 栅栏标记必须按**模板里实际出现的两种形态**中和，而不是只认"自闭合全形"：
#   起始形 ``<<<EVIDENCE``（不带结尾尖括号）
#   结束形 ``END_EVIDENCE>>>``（不带起始尖括号，且 ``END`` 与 ``EVIDENCE`` 以**下划线**相连）
# 只匹配自闭合形态会留下真实缺口：证据正文自行写入 ``END_EVIDENCE>>>`` 即可提前闭合
# 栅栏，把后续文本挤出"数据区"伪装成系统指令（间接注入，CLAUDE.md 10.3）。
# 因此两种形态都覆盖，并容忍空格/斜杠/下划线变体（``<<< / EVIDENCE >>>``、
# ``<<<END_EVIDENCE>>>``、``END EVIDENCE>>>``）；同时不牵连普通文本——
# 单个 ``<``/``>``、以及不含 ``EVIDENCE`` 词元的多重尖括号（如 ``a << b``、``>>>``）不受影响。
#
# 分隔符类必须覆盖**全部排版空格**（Zs），而不只是 ASCII 空格/制表符：
# 实测 ``END_EVIDENCE　>>>``（全角空格）与 ``END_EVIDENCE\xa0>>>``（NBSP）会原样穿透，
# 而对应的 ASCII 空格形态 ``END_EVIDENCE >>>`` 被中和——正则既然承认空白变体是有效的
# 栅栏同形串，就不能只认其中一种空白：漏掉 Zs 只是把同一攻击换成排版空格即可。
# U+3000 恰是中文公文语料的标准排版空格，对本仓库的语料不是冷门输入。
# 仍**不**接受 ``\n``/``\r``：换行不是可互换的排版空格，跨行拆开的标记不构成模板里的
# 栅栏词元，接受它们会跨行吞掉栅栏之间的正常正文。
# 转义序列而非字面量：NBSP/U+1680/U+2000-U+200A/U+202F/U+205F/U+3000 与 ASCII 空格在
# 源码里无可见区别，写成字面量会让评审无法确认类里究竟有没有它们（也无法 grep）。
# 转义序列而非字面量：NBSP/U+1680/U+2000-U+200A/U+202F/U+205F/U+3000 与 ASCII 空格在
# 源码里无可见区别，写成字面量会让评审无法确认类里究竟有没有它们（也无法 grep）。
_SEPARATOR = r"[ \t\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]"
_WORD_SEPARATOR = r"[ \t_\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]"

# 尖括号重复数**有界**且取**占有量词**（possessive，Python 3.11+）：
# ``<{2,}`` 在纯 ``<`` 串上会让引擎对每个起始位置回溯整段——实测 ``'<' * 16000``
# 单次调用 7.9 秒（4000 个 0.53 秒，呈二次增长）。该调用发生在 async 节点内的同步
# 路径上，等于阻塞事件循环（CLAUDE.md 3.2 / NO-05）。上界取 32：模板词元只有 3 个
# ``<``，更长的串本就不是栅栏同形串。占有量词让引擎不再把已吃的 ``<`` 吐回来——
# ``<`` 之后只允许分隔符，回溯永远不可能让匹配成立，所以"吐回"与"不吐回"的结果
# 完全一致，去掉的只是无用的指数级尝试。
#
# 结束形前面要求**词边界**（``(?<![0-9A-Za-z_])``）：``EVIDENCE>>>`` 是自足的结束标记，
# 若不在前面挡住 ASCII 词字符，普通文本 "at the weekend evidence>>> was lost" 会被
# 从 ``weekend`` 内部切开（``end evidence>>>`` 整体命中），把正文里的 ``weekend``
# 误伤成 ``week``。中文正文与 ```` 后紧跟标记（``政策END_EVIDENCE>>>``）不受影响——
# 中文不属于 ASCII 词字符，边界判定照样放行。
_FENCE_PATTERN = re.compile(
    # 起始形：2~32 个 '<'（占有量词，吃掉不回退）+ 可选斜杠 + 可选 END 前缀 + EVIDENCE
    r"<{2,32}+"
    + _SEPARATOR
    + r"*/?"
    + _SEPARATOR
    + r"*(?:END"
    + _WORD_SEPARATOR
    + r"*)?EVIDENCE"
    + _SEPARATOR
    + r"*>{0,32}"
    # 结束形：无起始尖括号，需 >=2 个 '>'，前面要求词边界防误伤长单词
    + r"|(?<![0-9A-Za-z_])(?:END"
    + _WORD_SEPARATOR
    + r"*)?EVIDENCE"
    + _SEPARATOR
    + r"*>{2,32}",
    re.IGNORECASE,
)

# 不可见字符按 Unicode 类别剥离，而不是枚举码位区间：
#   Cc（控制字符）与 Cf（格式字符：零宽、双向控制、BOM 等）。
# 用类别而非区间的原因：零宽字符可插进标记词元中间（``END␟_EVIDENCE``）让上面的确定性
# 匹配失效、剥离后又"复原"成可读标记，而 Cf 类的成员随 Unicode 版本持续增加，
# 枚举区间会漏；类别判定则自动覆盖新增成员。
# 保留 \t \n \r（同属 Cc，但提示词排版依赖它们）。
_ALLOWED_CONTROL_CHARS = frozenset("\t\n\r")
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf"})

# 但**类别判定本身也不完整**：Unicode 的 Default_Ignorable_Code_Point 里有一批码位
# 落在 Mn/Lo/Cn 类别，同样零宽无字形、同样能被插进标记词元中间，却躲开 Cc/Cf 判定。
# 实测（改这段代码前）：把下面任意一个码位插进 ``EVI␟DENCE`` 或 ``END_EVIDENCE␟>>>``
# 都以字节原样穿透，并把其后文本挤出栅栏。故显式列出：
#   U+034F CGJ、U+17B4-17B5 高棉固有元音、U+180B-180D/U+180F 蒙古文自由变体选择符、
#   U+FE00-FE0F 变体选择符 1-16、U+E0100-E01EF 变体选择符增补      —— 以上 Mn
#   U+115F-U+1160 Hangul 填充、U+3164 与 U+FFA0 填充符             —— 以上 Lo
#   U+2065、U+FFF0-FFF8、U+E0000-E001F、U+E0080-E00FF、U+E01F0-E0FFF —— 以上 Cn（未分配）
# （U+2065 是"保留供内部使用"的未分配码位，属该属性的成员但容易被逐项列举时漏掉——
# 它不是靠读类别就能想到的，是靠拿完整属性表逐码位对撞扫出来的。）
# 这里可以枚举、而 Cf 不行，是因为二者稳定性不同：Default_Ignorable_Code_Point 受
# Unicode 稳定性政策约束——既有码位的该属性不会改变，表只会因新码位而增补；而"哪些
# 字符算 Cf"是随版本在格式字符这一类里持续扩张的，只能靠类别判定。
# 不整体剥离 Mn/Lo：Mn 是正常文字的组成部分（重音、天城文元音符号），整体剥离会把
# "café" 写成 "cafe"、破坏证据正文（过度中和）；只取其中零宽无字形的成员。
_DEFAULT_IGNORABLE_EXTRA_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x2065, 0x2065),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0xE0000, 0xE001F),
    (0xE0080, 0xE00FF),
    (0xE0100, 0xE01EF),
    (0xE01F0, 0xE0FFF),
)
# 展开成码位集合而非保留区间：区间是给评审看的"来源"，查表要的是 O(1)——
# 逐字符对区间做 any() 在 65535 字符的证据上会明显拖慢同步路径。
_DEFAULT_IGNORABLE_EXTRA = frozenset(
    code_point
    for low, high in _DEFAULT_IGNORABLE_EXTRA_RANGES
    for code_point in range(low, high + 1)
)


def _is_invisible(char: str) -> bool:
    """判断单个字符是否为零宽无字形、可用来偷渡标记词元的字符。

    Args:
        char: 单个字符（调用方保证长度为 1）。

    Returns:
        命中 Default_Ignorable 附加表或 ``Cc``/``Cf`` 类别时为 ``True``。
    """
    return (
        ord(char) in _DEFAULT_IGNORABLE_EXTRA or unicodedata.category(char) in _INVISIBLE_CATEGORIES
    )


def _strip_invisible(text: str) -> str:
    """剥离控制字符、格式字符与其它零宽无字形的默认可忽略码位。

    Args:
        text: 待处理文本。

    Returns:
        去掉不可见字符后的文本；``\\t``、``\\n``、``\\r`` 保留（提示词排版依赖）。
    """
    return "".join(
        char for char in text if char in _ALLOWED_CONTROL_CHARS or not _is_invisible(char)
    )


def neutralize_untrusted(text: str) -> str:
    """中和不可信文本中的栅栏同形串与不可见字符。

    Args:
        text: 原始不可信文本（检索证据、工具结果等）。

    Returns:
        可直接嵌入 Prompt 的文本；长度变化仅发生在被中和的标记与不可见字符处。

    Notes:
        - 不做截断与语义改写——截断策略由调用方按上下文预算决定（CLAUDE.md 6.6）；
        - 本函数是**确定性**卫生层，只处理可枚举的标记形态与不可见字符，不做语义
          判断；同形字（homoglyph，如全角 ``＜``、数学角括号 ``⟨``）、自然语言式注入
          等由阶段二的 NeMo Guardrails / Llama Guard 承担（CLAUDE.md 10.3），
          不得据本函数宣称"注入已被完全阻断"。
    """
    # 顺序不可颠倒：先剥离不可见字符，再中和栅栏。
    # 反过来则存在真实缺口——以起始形为例，在 ``<<<`` 与 ``EVIDENCE`` 之间插入一个
    # 零宽空格（U+200B）后栅栏正则匹配不到（``<{2,}`` 与 ``EVIDENCE`` 之间只容许
    # 分隔符类），而随后的剥离会把这个零宽字符去掉、"复原"出一枚可读的 ``<<<EVIDENCE``，
    # 等于防线自拆。结束形不受零宽插入影响：``END`` 前缀在正则里可选，``EVIDENCE>>>``
    # 单独出现也能命中（实测验证：可忽略码位里仅起始形形态会因此泄漏）。
    return _FENCE_PATTERN.sub(_REDACTED, _strip_invisible(text))
