"""文本处理纯函数（无副作用，供知识层与智能体复用）。"""

from __future__ import annotations

import hashlib
import re

_WHITESPACE_PATTERN = re.compile(r"\s+")


def normalize_whitespace(text: str) -> str:
    """合并连续空白为单个空格并去首尾空白。

    Args:
        text: 原始文本。

    Returns:
        归一化后的文本。
    """
    return _WHITESPACE_PATTERN.sub(" ", text).strip()


def truncate_text(text: str, max_chars: int, *, suffix: str = "…") -> str:
    """按字符数截断文本（用于日志与展示裁剪，不用于切分）。

    Args:
        text: 原始文本。
        max_chars: 最大保留字符数（含 suffix）。
        suffix: 截断标记。

    Returns:
        长度不超过 ``max_chars`` 的文本；未超长时原样返回。

    Raises:
        ValueError: ``max_chars`` 小于等于 ``len(suffix)``。
    """
    if max_chars <= len(suffix):
        msg = f"max_chars({max_chars}) 必须大于 suffix 长度({len(suffix)})"
        raise ValueError(msg)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - len(suffix)] + suffix


def content_fingerprint(content: bytes) -> str:
    """计算内容指纹（SHA-256 十六进制），用于入库幂等键（源路径 + 内容 hash）。

    Args:
        content: 文件字节内容。

    Returns:
        64 位十六进制摘要。
    """
    return hashlib.sha256(content).hexdigest()
