"""流式前缀门：判定模型的「无据」声明是否出现在回答开头（CLAUDE.md 4.7 / NO-12）。

v2 提示词要求模型在**完全无法回答**时，把 ``【无据】`` 作为回答的第一个字符，
且标记之后不写任何内容。本模块把这条约定变成确定性判定：

- 只看回答的**首个非空白前缀**。前缀一旦与标记分歧，整段回答按正常作答放行，
  之后再出现同样字串也不改判——部分作答（先答有据部分、再说明未涵盖）的开头
  不是标记，结构上无法被误判成拒答。
- 判定过程中最多缓冲 ``len(marker)`` 个字符（外加前导空白），token 流几乎不受影响。
- 判为拒答后丢弃全部缓冲：模型即便在标记后继续生成，那段文字也不推给用户。

**为什么在流式路径上做这件事**：答案边生成边推 ``message.delta``，若等整段结束
再回头判断，用户已经看到了将被丢弃的正文。前缀门在首个分歧点之前不做任何推送，
既保住了低延迟，也保证用户不会看到"先输出再撤回"。
"""

from __future__ import annotations

from agent_platform.agents.gov_policy_qa.prompts import ANSWER_NO_EVIDENCE_MARKER


class NoEvidencePrefixGate:
    """按首个非空白前缀判定模型是否声明"证据完全无法支撑"。

    Args:
        marker: 无据声明标记；默认取提示词资产里的常量（v2 协议）。

    Raises:
        ValueError: 标记为空串（空标记会让任何回答都"匹配成功"）。
    """

    def __init__(self, marker: str = ANSWER_NO_EVIDENCE_MARKER) -> None:
        if not marker:
            raise ValueError("无据标记不得为空串")
        self._marker = marker
        self._buffer = ""
        self._is_refused = False
        self._is_open = False

    @property
    def is_refused(self) -> bool:
        """是否已判定为拒答（标记出现在首个非空白前缀之后不再改动）。"""
        return self._is_refused

    def feed(self, delta: str) -> str | None:
        """接收一个流式增量。

        Args:
            delta: 模型本次产出的文本增量。

        Returns:
            可以立即推送给用户的文本；``None`` 表示继续缓冲或已判拒答。
        """
        if self._is_refused:
            return None
        if self._is_open:
            return delta
        self._buffer += delta
        stripped = self._buffer.lstrip()
        if stripped.startswith(self._marker):
            self._is_refused = True
            self._buffer = ""
            return None
        if self._marker.startswith(stripped):
            # 仍是标记的真前缀（含纯空白）：继续缓冲，等下一个增量
            return None
        self._is_open = True
        released = self._buffer
        self._buffer = ""
        return released

    def flush(self) -> str:
        """流结束时释放剩余缓冲。

        Returns:
            尚未推送的文本；已判拒答时为空串（标记之后的正文一律丢弃）。
        """
        if self._is_refused:
            return ""
        released = self._buffer
        self._buffer = ""
        return released


__all__: list[str] = ["NoEvidencePrefixGate"]
