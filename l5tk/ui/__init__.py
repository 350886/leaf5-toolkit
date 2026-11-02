"""界面层：只依赖 ops 的公开方法，不碰 core 细节。"""

from .app import App, run

__all__ = ["App", "run"]
