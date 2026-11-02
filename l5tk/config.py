"""配置：所有可调值集中在这里，环境变量优先于默认值。

为什么不用 argparse 直接铺到底：GUI 也要用同一套值，参数解析只属于 cli。
环境变量前缀统一 L5TK_，避免和 V1 的 LEAF5_* 撞车。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ENV_PREFIX = "L5TK_"

# 三个候选目录：源码运行 / PyInstaller 解包 / EXE 所在目录。
# 顺序即优先级：EXE 同级目录优先，这样用户换 APK 不用重新打包。
def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _bundle_dir() -> Path:
    """PyInstaller 的 _MEIPASS；源码模式下等于项目根。"""
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        return Path(mp)
    return Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(ENV_PREFIX + name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _find_bin(name: str, subdir: str = "bin") -> str:
    """内嵌优先（EXE 同级 -> 打包内），最后退回 PATH。"""
    exe = name + (".exe" if os.name == "nt" else "")
    for base in (_exe_dir(), _bundle_dir()):
        p = base / "assets" / subdir / exe
        if p.is_file():
            return str(p)
    return exe  # 交给 PATH 查


@dataclass
class Config:
    # --- 外部依赖（可覆盖，默认找内嵌）---
    adb: str = field(default_factory=lambda: _env("ADB") or _find_bin("adb"))
    fastboot: str = field(default_factory=lambda: _env("FASTBOOT") or _find_bin("fastboot"))
    serial: str = field(default_factory=lambda: _env("SERIAL"))

    # --- 目录 ---
    base_dir: Path = field(default_factory=_exe_dir)
    bundle_dir: Path = field(default_factory=_bundle_dir)
    work_dir: Path = field(default_factory=lambda: Path(_env("WORK") or (_exe_dir() / "work")))
    log_dir: Path = field(default_factory=lambda: Path(_env("LOG_DIR") or (_exe_dir() / "logs")))

    # --- 超时（秒）。adb 在 Windows 上偶尔卡 usb 枚举，短超时比挂死好 ---
    timeout_short: int = field(default_factory=lambda: _env_int("TIMEOUT_SHORT", 15))
    timeout_normal: int = field(default_factory=lambda: _env_int("TIMEOUT", 90))
    timeout_flash: int = field(default_factory=lambda: _env_int("TIMEOUT_FLASH", 600))
    store_settle: float = field(
        default_factory=lambda: float(_env("STORE_SETTLE") or 3.0)
    )

    # --- 行为开关 ---
    plain_log: bool = field(default_factory=lambda: _env_bool("LOG_PLAIN", False))
    allow_downgrade: bool = field(default_factory=lambda: _env_bool("ALLOW_DOWNGRADE", True))
    dry_run: bool = field(default_factory=lambda: _env_bool("DRY_RUN", False))

    def ensure_dirs(self) -> None:
        for d in (self.work_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict[str, str]:
        return {
            "adb": self.adb,
            "fastboot": self.fastboot,
            "serial": self.serial or "(自动)",
            "work_dir": str(self.work_dir),
            "log_dir": str(self.log_dir),
            "dry_run": "on" if self.dry_run else "off",
        }


def load(**overrides) -> Config:
    """构造 Config，把 None 的覆盖项丢掉，避免把默认值冲成 None。"""
    cfg = Config()
    for k, v in overrides.items():
        if v is None:
            continue
        if not hasattr(cfg, k):
            raise AttributeError("Config 没有字段 %r" % k)
        setattr(cfg, k, v)
    return cfg
