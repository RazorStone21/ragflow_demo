"""评估 ``thread_id`` 的构造（CLAUDE.md 4.4）。

这个文件的存在理由是一次真实故障：``eval_thread_id`` 原本是
``f"eval-{case_id}-{run_id}"`` 直接拼接，样例 id 配上 29 字符的 run_id 后越过了
``compose_thread_id`` 的 64 字符**段**限。后果不是启动期报错，而是``--layer all``
下**每一条**样例都以 ``InvalidThreadIdError`` 记成"执行错误"、聚合分为空，而退出码
仍是一个规规矩矩的"失败"——读起来像"质量差"，不像"有 bug"。

两个性质必须由测试守住：
**段长**（与样例 id 长度无关，否则加一条长样例就再次炸）与
**唯一性**（两条样例共用一条 thread 会让后者读到前者的检查点，是静默串味）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_platform.core.ids import THREAD_ID_MAX_LEN, parse_thread_id
from agent_platform.evaluation.loader import load_golden_set
from agent_platform.evaluation.runner import EVAL_THREAD_USER, eval_thread_id

# 会话段的上限来自 core/ids.py 的 _SEGMENT_PATTERN；thread_id 总长另有限制。
_SESSION_MAX_LEN = 64

_LONG_CASE_ID = "boundary-" + "x" * 200
"""比任何真实样例都长的 id：段长必须与输入长度**无关**，否则加条长样例就再炸一次。"""


@pytest.mark.parametrize(
    "case_id",
    [
        "typical-coal-capacity-price-fixed-cost-ratio",
        _LONG_CASE_ID,
        "a",
        # 与真实黄金集里最长的 id 等长（防止将来缩短了固定开销又刚好卡在边缘）
        "boundary-guizhou-tariff-table-residential",
    ],
)
def test_session_segment_within_limit(case_id: str) -> None:
    """会话段恒不超限，且总长在 thread_id 上限之内。"""
    thread_id = eval_thread_id("demo", case_id, "eval-20261016T104622Z-daf4bc1d")
    _, _, session_id = parse_thread_id(thread_id)
    assert len(session_id) <= _SESSION_MAX_LEN, f"会话段 {len(session_id)} 字符：{session_id!r}"
    assert len(thread_id) <= THREAD_ID_MAX_LEN


def test_thread_id_remains_parseable_and_keeps_tenant() -> None:
    """租户段必须原样保留——检索隔离依赖它（NO-19）。"""
    thread_id = eval_thread_id("demo", _LONG_CASE_ID, "eval-20261016T104622Z-daf4bc1d")
    assert parse_thread_id(thread_id) == (
        "demo",
        EVAL_THREAD_USER,
        thread_id.split(":", 2)[2],
    )


def test_same_input_is_reproducible() -> None:
    """同样的 ``(样例, 轮次)`` 必须得到同一条 thread：重放要落回同一个检查点。"""
    first = eval_thread_id("demo", "typical-x", "eval-run-a")
    second = eval_thread_id("demo", "typical-x", "eval-run-a")
    assert first == second


def test_different_run_gets_a_fresh_thread() -> None:
    """换一轮必须换 thread，否则上一轮的检查点会被这一轮读到。"""
    first = eval_thread_id("demo", "typical-x", "eval-run-a")
    second = eval_thread_id("demo", "typical-x", "eval-run-b")
    assert first != second


def test_shared_prefix_does_not_collide() -> None:
    """截断前缀相同的样例不得撞车——这正是"截断 + 不加摘要"会踩的坑。"""
    left = eval_thread_id("demo", _LONG_CASE_ID + "-alpha", "eval-run-a")
    right = eval_thread_id("demo", _LONG_CASE_ID + "-beta", "eval-run-a")
    assert left != right


def test_committed_golden_set_ids_all_fit() -> None:
    """**随仓发布的**黄金集必须整体可构造（守着真实产物，而不只是构造出来的样例）。"""
    golden = load_golden_set(Path("tests/evals/golden"))
    run_id = "eval-20261016T104622Z-daf4bc1d"
    thread_ids = [eval_thread_id("demo", case.id, run_id) for case in golden.cases]
    assert len(set(thread_ids)) == len(thread_ids), "黄金集内出现了重复的 thread_id"
    for case, thread_id in zip(golden.cases, thread_ids, strict=True):
        _, _, session_id = parse_thread_id(thread_id)
        assert len(session_id) <= _SESSION_MAX_LEN, f"{case.id} 超限：{thread_id!r}"
