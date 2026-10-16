"""黄金集装载器的单元测试（CLAUDE.md 9.2 / 9.3）。

重点不是"能不能读出来"，而是**坏输入是否显式失败**：黄金集静默变短会直接
变成门禁的假绿（NO-06）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_platform.core.errors import GoldenSetError
from agent_platform.evaluation.loader import (
    coverage_report,
    load_golden_set,
    unknown_statuses,
)
from agent_platform.evaluation.schemas import CaseCategory


def _case(case_id: str, **overrides: object) -> dict[str, object]:
    """构造一条最小合法样例；``**overrides`` 用于制造非法输入。"""
    payload: dict[str, object] = {
        "id": case_id,
        "category": CaseCategory.TYPICAL.value,
        "capability": "单文档事实检索",
        "question": f"问题 {case_id}",
        "expected_status": "answered",
        "draft": False,
        "reviewed_by": "reviewer",
    }
    payload.update(overrides)
    return payload


def _write(directory: Path, name: str, rows: list[dict[str, object]]) -> Path:
    path = directory / name
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8"
    )
    return path


def test_loads_cases_from_all_files_sorted_by_id(tmp_path: Path) -> None:
    """跨文件装载，并按 id 排序（顺序稳定，产物才可逐例 diff）。"""
    _write(tmp_path, "b.jsonl", [_case("zzz-second"), _case("aaa-first")])
    _write(tmp_path, "a.jsonl", [_case("mmm-middle")])

    golden = load_golden_set(tmp_path)

    assert [c.id for c in golden.cases] == ["aaa-first", "mmm-middle", "zzz-second"]


def test_blank_lines_are_ignored_but_line_numbers_stay_true(tmp_path: Path) -> None:
    """空行忽略，但**报错行号仍按原文件计**——否则定位会差行。"""
    path = tmp_path / "g.jsonl"
    path.write_text(
        json.dumps(_case("ok-one"), ensure_ascii=False) + "\n\n{坏行}\n", encoding="utf-8"
    )

    with pytest.raises(GoldenSetError) as excinfo:
        load_golden_set(tmp_path)

    assert "g.jsonl:3" in str(excinfo.value)


def test_duplicate_id_across_files_is_rejected(tmp_path: Path) -> None:
    """id 重复必须拒绝：它是基线对比的键，重复会让分数互相覆盖。"""
    _write(tmp_path, "a.jsonl", [_case("dup-id")])
    _write(tmp_path, "b.jsonl", [_case("dup-id")])

    with pytest.raises(GoldenSetError) as excinfo:
        load_golden_set(tmp_path)

    assert "dup-id" in str(excinfo.value)
    assert "a.jsonl:1" in excinfo.value.detail
    assert "b.jsonl:1" in excinfo.value.detail


def test_missing_directory_is_rejected(tmp_path: Path) -> None:
    """目录不存在时显式失败，而不是返回一个"零样例的黄金集"。"""
    with pytest.raises(GoldenSetError, match="目录不存在"):
        load_golden_set(tmp_path / "nope")


def test_empty_directory_is_rejected(tmp_path: Path) -> None:
    """目录在但没有任何 jsonl：同样显式失败（零样例跑出来的 100% 是最危险的绿灯）。"""
    with pytest.raises(GoldenSetError, match="没有"):
        load_golden_set(tmp_path)


def test_field_validation_error_names_the_field_and_location(tmp_path: Path) -> None:
    """字段校验失败要指名道姓：哪个文件、哪一行、哪个字段。"""
    _write(tmp_path, "g.jsonl", [_case("bad-cat", category="not-a-category")])

    with pytest.raises(GoldenSetError) as excinfo:
        load_golden_set(tmp_path)

    assert "g.jsonl:1" in str(excinfo.value)
    assert "category" in str(excinfo.value)


def test_non_object_line_is_rejected(tmp_path: Path) -> None:
    """一行是 JSON 但不是一个对象（如数组）——同样显式失败。"""
    (tmp_path / "g.jsonl").write_text("[1, 2, 3]\n", encoding="utf-8")

    with pytest.raises(GoldenSetError, match="必须是 JSON 对象"):
        load_golden_set(tmp_path)


def test_draft_and_reviewed_are_split(tmp_path: Path) -> None:
    """草稿与已签署分流；门禁只统计已签署的。"""
    _write(
        tmp_path,
        "g.jsonl",
        [
            _case("reviewed-one"),
            {
                "id": "draft-one",
                "category": CaseCategory.BOUNDARY.value,
                "capability": "表格题",
                "question": "草稿题",
                "expected_status": "answered",
            },
        ],
    )

    golden = load_golden_set(tmp_path)

    assert [c.id for c in golden.reviewed] == ["reviewed-one"]
    assert [c.id for c in golden.drafts] == ["draft-one"]


def test_draft_flag_with_reviewer_is_rejected(tmp_path: Path) -> None:
    """``draft=True`` 与已签署不能并存——两者都表示"算不算数"，共存即漂移入口。"""
    _write(tmp_path, "g.jsonl", [_case("confused", draft=True, reviewed_by="someone")])

    with pytest.raises(GoldenSetError) as excinfo:
        load_golden_set(tmp_path)

    assert "draft" in str(excinfo.value)


def test_reference_answer_requires_source(tmp_path: Path) -> None:
    """有参考答案就必须有出处（§9.2 不得自评自满的落点）。"""
    _write(tmp_path, "g.jsonl", [_case("no-source", reference_answer="某个结论")])

    with pytest.raises(GoldenSetError) as excinfo:
        load_golden_set(tmp_path)

    assert "reference_answer_source" in str(excinfo.value)


def test_fingerprint_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    """指纹对内容敏感、对读取顺序不敏感——金标文件被改动必须能看出来。"""
    (tmp_path / "a.jsonl").write_text(
        json.dumps(_case("one"), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    first = load_golden_set(tmp_path).sha256
    again = load_golden_set(tmp_path).sha256
    assert first == again

    (tmp_path / "a.jsonl").write_text(
        json.dumps(_case("one", question="改了措辞"), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    assert load_golden_set(tmp_path).sha256 != first


def test_coverage_report_counts_and_flags_missing_categories(tmp_path: Path) -> None:
    """覆盖度报告把"缺哪一类"作为事实报出来，渲染文本可读。"""
    _write(
        tmp_path,
        "g.jsonl",
        [
            _case("t1"),
            _case("t2", category=CaseCategory.SECURITY.value),
            {
                "id": "d1",
                "category": CaseCategory.REFUSAL.value,
                "capability": "语料外问题",
                "question": "草稿",
                "expected_status": "insufficient_evidence",
            },
        ],
    )

    report = coverage_report(load_golden_set(tmp_path))

    assert report.total == 3
    assert report.reviewed == 2
    assert report.drafts == 1
    assert report.by_category[CaseCategory.TYPICAL] == 1
    assert report.by_category[CaseCategory.SECURITY] == 1
    assert report.missing_categories == (CaseCategory.BOUNDARY,)
    assert "⚠ 类别空缺：boundary" in report.render()


def test_unknown_statuses_is_empty_for_valid_set(tmp_path: Path) -> None:
    """合法集合下没有未知状态（该函数是放宽字段类型时的兜底告警）。"""
    _write(tmp_path, "g.jsonl", [_case("t1"), _case("r1", expected_status="insufficient_evidence")])

    assert unknown_statuses(load_golden_set(tmp_path)) == ()


def test_by_id_returns_none_for_unknown(tmp_path: Path) -> None:
    """``by_id`` 未命中返回 None，由调用方决定报错还是跳过。"""
    _write(tmp_path, "g.jsonl", [_case("known")])

    golden = load_golden_set(tmp_path)

    assert golden.by_id("known") is not None
    assert golden.by_id("nope") is None


def test_unreadable_entry_is_reported_not_swallowed(tmp_path: Path) -> None:
    """目录里有个叫 ``x.jsonl`` 的**目录**：读不出来就报错，不得静默跳过（NO-06）。"""
    _write(tmp_path, "good.jsonl", [_case("ok")])
    (tmp_path / "trap.jsonl").mkdir()

    with pytest.raises(GoldenSetError, match="不可读"):
        load_golden_set(tmp_path)


def test_render_has_no_warning_when_all_categories_present(tmp_path: Path) -> None:
    """四类齐备时不打印空缺告警——告警常亮就等于没有告警。"""
    _write(
        tmp_path,
        "g.jsonl",
        [_case(f"{category.value}-01", category=category.value) for category in CaseCategory],
    )

    report = coverage_report(load_golden_set(tmp_path))

    assert report.missing_categories == ()
    assert "⚠" not in report.render()
    assert "boundary=1" in report.render()
