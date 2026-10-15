"""注册表一致性：注册表 ↔ langgraph.json ↔ 入口模块三者同源（CLAUDE.md 4.6 / 4.8 / NO-20）。"""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from agent_platform.agents.gov_policy_qa import graph as qa_graph
from agent_platform.agents.gov_policy_qa import state as qa_state
from agent_platform.agents.registry import (
    AGENTS,
    GUARDRAIL_LEVEL_STANDARD,
    AgentSpec,
    agent_names,
    get_agent,
)
from agent_platform.core.errors import ErrorCode, NotFoundError, ValidationFailedError

_REPO_ROOT = Path(__file__).resolve().parents[2]
"""仓库根目录（本文件位于 tests/unit/，上溯两级）。"""
_LANGGRAPH_JSON = _REPO_ROOT / "langgraph.json"
"""LangGraph 部署声明；其 graphs 条目必须与注册表指向同一入口。"""


def _langgraph_payload() -> dict[str, Any]:
    """读取并解析仓库根 langgraph.json。"""
    payload = json.loads(_LANGGRAPH_JSON.read_text(encoding="utf-8"))
    return cast(dict[str, Any], payload)


def _declared_graphs() -> dict[str, str]:
    """返回 langgraph.json 的 graphs 映射（智能体名 → 入口字符串）。"""
    graphs = _langgraph_payload()["graphs"]
    return cast(dict[str, str], graphs)


def _spec(**overrides: Any) -> AgentSpec:
    """由首条注册项派生临时 AgentSpec（model_copy 不做字段校验，仅用于路径/格式类断言）。"""
    return AGENTS[0].model_copy(update=overrides)


def _write_field(target: object, field_name: str, value: object) -> None:
    """按变量名写字段，用于运行期验证 frozen（变量名可绕过 lint 的常量 setattr 检查）。"""
    setattr(target, field_name, value)


def test_langgraph_json_exists_and_declares_graphs() -> None:
    """仓库根 langgraph.json 存在，且 graphs 是非空的"名称→入口字符串"映射。"""
    assert _LANGGRAPH_JSON.is_file()
    graphs = _langgraph_payload().get("graphs")
    assert isinstance(graphs, dict), "langgraph.json 必须包含 graphs 对象"
    assert graphs, "graphs 不得为空：注册过的智能体必须可被 LangGraph 加载"
    assert all(
        isinstance(name, str) and isinstance(entry, str) for name, entry in graphs.items()
    ), "graphs 必须是 名称→入口字符串 的映射"


def test_registry_and_langgraph_declare_same_names() -> None:
    """注册名与 langgraph.json 键双向一致：注册了必须声明，声明了必须注册。"""
    registered = set(agent_names())
    declared = set(_declared_graphs())
    assert registered, "注册表不得为空"
    assert registered == declared, (
        f"仅注册未声明: {sorted(registered - declared)};"
        f" 仅声明未注册: {sorted(declared - registered)}"
    )


def test_langgraph_entrypoint_matches_registry_entrypoint() -> None:
    """同名条目的入口与 spec.entrypoint **逐字相同**（registry 模块 docstring 的承诺）。

    不做 "./" 前缀容忍：两处声明是同一份契约的两种写法，容忍差异会让"改了一处忘了
    另一处"静默通过。前缀差异已由 ``import_ref`` 在加载侧吸收。
    """
    declared = _declared_graphs()
    for spec in AGENTS:
        entry = declared[spec.name]
        assert entry == spec.entrypoint, (
            f"{spec.name} 入口声明漂移: langgraph.json={entry!r}, registry={spec.entrypoint!r}"
        )


def test_entrypoint_files_exist_under_repo_root() -> None:
    """两处入口声明的文件部分都相对仓库根真实存在（防"注册了但跑不起来"）。"""
    entries = [spec.entrypoint for spec in AGENTS]
    entries.extend(_declared_graphs().values())
    for entry in entries:
        block_path, _, factory_name = entry.partition(":")
        assert factory_name, f"入口缺少工厂名（应为 路径.py:函数 形式）: {entry}"
        assert (_REPO_ROOT / block_path).is_file(), f"入口文件不存在: {block_path}"


def test_load_factory_returns_entry_module_build_graph() -> None:
    """load_factory() 返回入口模块里同一个 build_graph 对象（可调用且具名 build_graph）。"""
    for spec in AGENTS:
        module_name, _, attribute = spec.import_ref.partition(":")
        module = importlib.import_module(module_name)
        factory = spec.load_factory()
        assert callable(factory)
        assert factory is getattr(module, attribute)
        assert getattr(factory, "__name__", None) == "build_graph"


def test_build_graph_injects_dependencies_as_keyword_only() -> None:
    """入口工厂的依赖全部关键字显式注入：四参数齐全且无任何位置参数（CLAUDE.md 4.8）。"""
    parameters = inspect.signature(qa_graph.build_graph).parameters
    for required in ("retriever", "chat", "chat_logical_name", "checkpointer"):
        assert required in parameters, f"build_graph 缺少注入依赖: {required}"
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in parameters.values()
    ), "build_graph 不得接受位置参数"


def test_get_agent_unknown_name_raises_not_found() -> None:
    """未知名称抛 NotFoundError 且错误码为 NOT_FOUND（映射 404，不泄露内部清单）。"""
    unknown = "definitely_not_registered_agent"
    assert unknown not in agent_names()
    with pytest.raises(NotFoundError) as excinfo:
        get_agent(unknown)
    assert excinfo.value.code is ErrorCode.NOT_FOUND


def test_get_agent_returns_the_registered_spec() -> None:
    """按名取到注册清单中的同一个 AgentSpec 对象（查询与清单同源，非拷贝）。"""
    by_name = {spec.name: spec for spec in AGENTS}
    spec = get_agent("gov_policy_qa")
    assert spec is by_name["gov_policy_qa"]


def test_agent_spec_is_frozen() -> None:
    """已构造的 AgentSpec 运行期不可写：给字段赋值抛 ValidationError（防注册信息被篡改）。"""
    with pytest.raises(ValidationError):
        _write_field(AGENTS[0], "owner", "别的团队")


@pytest.mark.parametrize("bad_version", ["1.0", "v1.0.0", "1.0.0-beta"])
def test_version_pattern_rejects_non_semver(bad_version: str) -> None:
    """version 必须严格匹配 MAJOR.MINOR.PATCH：非法版本在构造期即被拒（pattern 真生效）。"""
    with pytest.raises(ValidationError):
        AgentSpec(
            name="probe", owner="测试组", version=bad_version, entrypoint="src/probe/graph.py:f"
        )


def test_version_pattern_accepts_strict_semver() -> None:
    """合法语义化版本（1.2.3）构造成功且原值保留。"""
    spec = AgentSpec(
        name="probe", owner="测试组", version="1.2.3", entrypoint="src/probe/graph.py:f"
    )
    assert spec.version == "1.2.3"


@pytest.mark.parametrize(
    "entrypoint",
    [
        pytest.param("src/a/b/c.py:build_graph", id="无前缀形式"),
        # langgraph.json 与 registry 采用的写法；两处逐字一致，前缀必须在此被吸收
        pytest.param("./src/a/b/c.py:build_graph", id="CLI 惯用的 ./ 前缀形式"),
    ],
)
def test_import_ref_rewrites_to_importable_module_reference(entrypoint: str) -> None:
    """import_ref 归一到 importlib 可加载的 a.b.c:build_graph（两种写法同源同解）。"""
    assert _spec(entrypoint=entrypoint).import_ref == "a.b.c:build_graph"


def test_load_factory_rejects_entrypoint_without_factory_name() -> None:
    """入口缺少 ":工厂名" 时抛 ValidationFailedError（配置格式错误，属 422 语义而非 404）。"""
    spec = _spec(entrypoint="src/agent_platform/agents/gov_policy_qa/graph.py")
    with pytest.raises(ValidationFailedError):
        spec.load_factory()


def test_load_factory_raises_not_found_for_missing_module() -> None:
    """入口模块不可导入时抛 NotFoundError 且码为 NOT_FOUND（部署包不完整，可被运维定位）。"""
    spec = _spec(entrypoint="src/no_such_pkg/m.py:build_graph")
    with pytest.raises(NotFoundError) as excinfo:
        spec.load_factory()
    assert excinfo.value.code is ErrorCode.NOT_FOUND


def test_load_factory_raises_not_found_for_missing_factory() -> None:
    """模块存在但工厂属性缺失（取到 None/不可调用）时抛 NotFoundError。"""
    spec = _spec(entrypoint="src/agent_platform/core/errors.py:no_such_factory")
    with pytest.raises(NotFoundError):
        spec.load_factory()


def test_name_and_version_are_single_source_across_layers() -> None:
    """注册名 == state.AGENT_NAME == 入口目录名，且 spec.version == graph.AGENT_VERSION。"""
    spec = get_agent("gov_policy_qa")
    assert spec.name == qa_state.AGENT_NAME
    assert Path(spec.entrypoint.partition(":")[0]).parent.name == spec.name
    assert spec.version == qa_graph.AGENT_VERSION


def test_gov_policy_qa_has_no_tools_and_known_guardrail_level() -> None:
    """纯检索问答不挂任何工具（NO-11），护栏等级限于注册表导出的已知常量。"""
    spec = get_agent("gov_policy_qa")
    assert spec.tools == ()
    known_guardrail_levels = {GUARDRAIL_LEVEL_STANDARD}
    assert spec.guardrail_level in known_guardrail_levels, (
        f"未知护栏等级 {spec.guardrail_level!r}；已知: {sorted(known_guardrail_levels)}"
    )


def test_agent_names_are_unique() -> None:
    """注册名无重复（重名会让按名映射静默覆盖、并与 langgraph.json 键冲突）。"""
    names = agent_names()
    assert len(set(names)) == len(names)
