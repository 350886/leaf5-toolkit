"""说明文档与界面的一致性。

不检查文笔，只检查**该说的都说了** —— 文档最常见的坏法不是写错，是加了按钮忘了写。

不用 tkinter，所以能在主套件里跑（3.13 那个解释器没装 tk）。

    python -m pytest tests/test_help.py
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from l5tk.ui import helpdoc

APP = Path(__file__).resolve().parents[1] / "l5tk" / "ui" / "app.py"


def _labels() -> list[str]:
    """从 app.py 里刮出左栏按钮的文案（`("④ 取原厂 boot", self.act_payload,` 这种）。

    直接读源码而不是 import app：import 会拖 tkinter，还要一个能起窗口的环境。
    按钮文案是界面契约，改了就该同步改文档 —— 这个测试就是那道闸。
    """
    src = APP.read_text(encoding="utf-8")
    return re.findall(r'\("([^"]{2,24})",\s*self\.act_\w+', src)


def _doc() -> str:
    return helpdoc.plain()


def test_sections_not_empty():
    assert len(helpdoc.sections()) >= 8
    for name, body in helpdoc.sections():
        assert name.strip(), "章节名不能空"
        assert len(body) > 120, "章节「%s」太短，多半是占位" % name


def test_every_button_documented():
    """每个左栏按钮都要在说明里出现。"""
    labels = _labels()
    assert labels, "没从 app.py 刮到按钮文案，正则或代码结构变了"
    doc = _doc()
    missing = [t for t in labels if t not in doc]
    assert not missing, "这些按钮在说明里没有章节：%s" % "、".join(missing)


def test_menu_shortcuts_documented():
    doc = _doc()
    for key in ("F1", "Ctrl+F", "Ctrl+L", "Ctrl+Q"):
        assert key in doc, "快捷键 %s 没写进说明" % key


def test_three_routes_explained():
    """最快的三件事必须说清：三条写入通路、is-userspace 判据、init_boot 优先。"""
    doc = _doc()
    for word in ("fastbootd", "is-userspace", "init_boot", "unknown command"):
        assert word in doc, "说明里缺关键概念：%s" % word


def test_plain_is_stable():
    """导出用的是 plain()，两次调用必须一致（没有时间戳之类会漂的东西）。"""
    assert helpdoc.plain() == helpdoc.plain()
    assert "使用说明" in helpdoc.plain().splitlines()[0]


@pytest.mark.parametrize("word,expect", [
    ("fastbootd", True),
    ("init_boot", True),
    ("zzz-nothing-here", False),
])
def test_search(word, expect):
    hits = helpdoc.search(word)
    assert bool(hits) is expect
    for idx, line in hits:
        assert 0 <= idx < len(helpdoc.sections())
        assert 0 <= line < len(helpdoc.sections()[idx][1].splitlines())


def test_search_is_case_insensitive():
    assert helpdoc.search("FASTBOOTD") == helpdoc.search("fastbootd")


def test_search_empty():
    assert helpdoc.search("") == []
    assert helpdoc.search("   ") == []
