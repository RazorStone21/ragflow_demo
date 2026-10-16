"""评估层的依赖方向（CLAUDE.md 2.3 / 9.3）。

§2.3 的依赖方向是**单向向内**：``api → agents → core/tools/knowledge → utils``。
评估层是第 2.3 节表格里后加的模块，规范里没写它站哪——本仓的约定是它**只向内**
（``evaluation → agents/core/knowledge/settings``），**任何运行时模块都不得 import
它**。理由不是洁癖：评估层带着黄金集、阈值与裁判，一旦被 ``api/`` 或 ``agents/``
引入，生产镜像就多背一份评估资产，而且"生产路径依赖评估代码"会让评估的独立性
（判别人而不是被自己人判）彻底消失。

**为什么用 AST 扫而不是靠人看**：这类越界不会有任何运行时症状——反向 import 照样
能跑通，只有在拆包、做镜像瘦身或想单独演进评估层时才暴露，而那时改动面已经很大。
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path("src/agent_platform")
_EVALUATION_DIR = _SRC / "evaluation"
_EVALUATION_PACKAGE = "agent_platform.evaluation"


def _imported_modules(path: Path) -> set[str]:
    """静态收集一个文件 import 的全部模块名（含 ``from x import y`` 与函数内 import）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _runtime_modules() -> list[Path]:
    """``src/`` 下除评估层自身以外的全部模块。

    按**目录**排除，而不是拿 ``_EVALUATION_PACKAGE``（带点的模块名）去比对文件路径：
    文件路径里没有 ``agent_platform.evaluation`` 这串，那样写等于没排除，评估层
    内部文件之间的正常互引用会被当成越界报出来。
    """
    return [path for path in _SRC.rglob("*.py") if _EVALUATION_DIR not in path.parents]


def test_no_runtime_module_imports_the_evaluation_layer() -> None:
    """反向依赖必须为零，且逐个文件给出越界处，而不是只说"有依赖"。"""
    offenders: list[str] = []
    for path in _runtime_modules():
        imports = {
            name
            for name in _imported_modules(path)
            if name == _EVALUATION_PACKAGE or name.startswith(f"{_EVALUATION_PACKAGE}.")
        }
        if imports:
            offenders.append(f"{path.as_posix()}: {sorted(imports)}")

    assert not offenders, "运行时模块反向依赖了评估层：\n" + "\n".join(offenders)


def test_the_scan_actually_sees_the_evaluation_package() -> None:
    """扫描范围必须真的覆盖到评估层，否则上一条断言会因为"扫了个空"而永远通过。

    这类"守卫自己的守卫"常被当成多余——直到有人把 ``_SRC`` 改成不存在的路径、
    或评估层被挪走，上一条测试便静默地什么都不检查了。
    """
    assert _runtime_modules(), "运行时模块列表为空，扫描范围写错了"
    assert (_EVALUATION_DIR / "scoring.py").is_file()


def test_scan_finds_a_planted_reverse_import(tmp_path: Path) -> None:
    """用一个**真的含反向 import** 的文件验证扫描器认得出来。

    注入一个假样本，而不是相信扫描器的实现：若 ``_imported_modules`` 哪天只认
    ``import x`` 而漏掉 ``from x import y``，前两条断言恰好可能仍然通过（因为
    真实代码里没有反向依赖），缺陷就此潜伏。
    """
    planted = tmp_path / "offender.py"
    planted.write_text(
        "from agent_platform.evaluation.scoring import score_case\n"
        "import agent_platform.evaluation.reporting\n",
        encoding="utf-8",
    )
    imports = _imported_modules(planted)
    assert any(name.startswith(_EVALUATION_PACKAGE) for name in imports)
