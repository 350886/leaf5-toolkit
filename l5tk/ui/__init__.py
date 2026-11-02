"""界面层：只依赖 ops 的公开方法，不碰 core 细节。"""

from __future__ import annotations

__all__ = ["App", "run"]


def __getattr__(name: str):
    """延迟导入 App / run。

    ui 包里只有 app 需要 tkinter。包级 `__init__` 直接 `from .app import App`
    会把 tkinter 拖进每个想用纯数据模块（helpdoc）的地方 —— 3.13 那个解释器
    没装 tk，一 import 就炸，测试连收集都过不去。
    """
    if name in __all__:
        from .app import App, run
        return {"App": App, "run": run}[name]
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
