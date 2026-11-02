"""Google Play：装 / 拉起 / 判定能不能真的用。

V1 在真机上踩出来的结论，这里是硬编码的前提，不是猜测：
  * 商店装得上不等于能用。V1 时期实测：42.0.18-31 可用，44.6.15 装得上但一开就退。
    V2 期间同一台 Leaf5 上已经是 53.7.23-31 并在前台正常运行 —— 版本可用性随
    ONYX 的拦截策略变化，所以这里不写死版本号，只把「装上了还要验证真的能起来」当规矩。
  * ONYX 的 services.jar 在 gms_enable=false 时会拦 com.android.vending 启动，
    抛 SecurityException，异常抛在 system_server 侧，`am start` 的输出里看不到。
    所以判定必须「am start 之后等几秒，再看前台 Activity」，两条都要。
  * 把 gms_enable 写进 MMKV 是错路：mmap 文件带锁与 CRC，改坏就毁整份配置。
    账号验证通过时系统自己会置位，工具只负责装包与拉起。
"""

from __future__ import annotations

import time
from pathlib import Path

from ..core.adb import Adb
from ..core.result import ExecResult, Outcome
from ..logx import Logger

STORE = "com.android.vending"
GMS = "com.google.android.gms"
GSF = "com.google.android.gsf"
STORE_ACT = STORE + "/.AssetBrowserActivity"

# 账号页：先精确到「添加 Google 账号」，再退到账号总页，最后退到设置首页。
# 每上一档少一个参数，因为不同 Android 版本对 EXTRA 的支持不一样。
ACCOUNT_INTENTS = (
    ("android.settings.ADD_ACCOUNT_SETTINGS", ["--es", "account_types", "com.google"]),
    ("android.settings.ADD_ACCOUNT_SETTINGS", []),
    ("android.settings.SETTINGS", []),
)


def candidate_apks(base_dir: Path | None = None, explicit: str | Path | None = None,
                   include_defaults: bool = True) -> list[Path]:
    """按优先级列候选 APK。

    规则是「目录顺序即优先级，目录内取体积最大的那个」。
    V1 一开始全局按体积排序，结果用户换了个小体积的新版，被内置的 64MB 旧版挤掉，
    装回去还是旧版 —— 这个 bug 现场很难看出来，所以顺序必须由目录决定。

    include_defaults=False 只查 base_dir，测试用（否则会扫到仓库里真有的 APK）。
    """
    if explicit:
        p = Path(explicit)
        return [p] if p.is_file() else []

    dirs: list[Path] = []
    if base_dir:
        # EXE 同级的 apk\ 是用户唯一能直接换的地方，优先级必须最高。
        # assets/apk 是打进包里的内置副本，只在用户没放东西时兜底。
        dirs += [base_dir / "apk", base_dir / "assets" / "apk"]
    if include_defaults:
        dirs.append(Path.cwd() / "apk")
        try:
            from ..config import Config
            dirs.append(Config().bundle_dir / "assets" / "apk")
        except Exception:
            pass

    out: list[Path] = []
    seen: set[Path] = set()
    for d in dirs:
        try:
            d = d.resolve()
        except OSError:
            continue
        if d in seen or not d.is_dir():
            continue
        seen.add(d)
        apks = [p for p in d.glob("*.apk") if p.is_file() and p.stat().st_size > 1024 * 1024]
        if apks:
            out.append(max(apks, key=lambda p: p.stat().st_size))
    return out


def find_apk(base_dir: Path | None = None, explicit: str | Path | None = None,
             include_defaults: bool = True) -> Path:
    found = candidate_apks(base_dir, explicit, include_defaults)
    if not found:
        from ..core.errors import GpError
        raise GpError(
            "没找到商店 APK",
            hint="把 Phonesky_*.apk 放进 EXE 同级的 apk\\ 目录，或用 --apk 指定",
        )
    return found[0]


class GPlay:
    def __init__(self, adb: Adb, log: Logger):
        self.adb = adb
        self.log = log

    # ---------------- 状态 ----------------
    def state(self) -> dict:
        installed = self.adb.installed(STORE)
        return {
            "store": installed,
            "version": self.adb.version_name(STORE) if installed else "",
            "gms": self.adb.installed(GMS),
            "gsf": self.adb.installed(GSF),
            "foreground": self.adb.foreground_pkg(),
        }

    # ---------------- 动作 ----------------
    def install(self, apk: Path, reinstall: bool = True) -> ExecResult:
        apk = Path(apk)
        if not apk.is_file():
            from ..core.errors import GpError
            raise GpError("APK 不存在：%s" % apk)
        size_mb = apk.stat().st_size / 1024 / 1024
        self.log.info("安装 %s（%.1f MB）" % (apk.name, size_mb))
        r = self.adb.install(str(apk), reinstall=reinstall)
        if not r.ok:
            text = r.text
            hint = ""
            if "VERSION_DOWNGRADE" in text:
                hint = "目标版本低于已装版本，加 -d 重试（默认已带）"
            elif "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in text:
                hint = "签名不一致，需要先 adb uninstall com.android.vending"
            elif "INSUFFICIENT_STORAGE" in text:
                hint = "设备空间不足"
            from ..core.errors import GpError
            raise GpError("安装失败：%s" % (text.strip()[:300] or "无输出"), hint=hint)
        return r

    def open_account_page(self) -> str:
        """打开添加 Google 账号的页面，返回实际用上的 action；全失败返回空。"""
        for action, extra in ACCOUNT_INTENTS:
            r = self.adb.run("shell", "am", "start", "-a", action, *extra)
            if r.ok and "Error" not in r.text and "Exception" not in r.text:
                return action
        return ""

    def launch_store(self, settle: float = 3.0) -> tuple[bool, str]:
        """拉起商店并判断它是否真的留在前台。

        返回 (ok, detail)。detail 在失败时是一句人话原因。
        """
        r = self.adb.start(STORE_ACT, wait=True)
        if "Error:" in r.text or "does not exist" in r.text:
            # 部分版本没有 AssetBrowserActivity，退到 LAUNCHER intent
            r = self.adb.run("shell", "monkey", "-p", STORE,
                             "-c", "android.intent.category.LAUNCHER", "1")
        time.sleep(max(0.5, settle))
        fg = self.adb.foreground_pkg()
        if fg == STORE:
            return True, "商店在前台"
        if not fg:
            return False, "读不到前台 Activity（dumpsys 被限制？）"
        return False, "前台是 %s，商店被挡回去了" % fg

    def enable(self, apk: str | Path | None = None, base_dir: Path | None = None,
               reinstall_if_blocked: bool = True) -> Outcome:
        """一条龙：没装就装 → 拉起 → 判定。

        stage 的取值和含义（GUI 按它决定显示什么按钮）：
            installed   刚装完，还没拉起
            launched    商店已在前台，可用
            need_login  被 ONYX 拦了，需要先在设置里登录并验证 Google 账号
            blocked     装的是不兼容版本，重装也没救
            failed      其它失败，看 detail
        """
        st = self.state()
        if not st["store"]:
            apk_path = Path(apk) if apk else find_apk(base_dir)
            self.install(apk_path)
            st = self.state()
            if not st["store"]:
                return Outcome(False, "failed", "安装命令返回成功但包仍不在，pm 路径读不到")

        if not st["gms"]:
            self.log.warn("GMS 不在，商店大概率起不来（多数 ROM 已预置，缺失说明系统被裁过）")

        ok, detail = self.launch_store(self.adb.cfg.store_settle)
        if ok:
            return Outcome(True, "launched", detail, data={"version": st["version"]})

        # 被挡：先看是不是账号没验证。ONYX 的 gms_enable 由账号验证触发。
        action = self.open_account_page()
        if action:
            self.log.warn(detail)
            self.log.info("已打开账号页（%s），登录并完成验证后商店才会放行" % action)
            return Outcome(
                False, "need_login",
                "商店被系统拦下，需要登录 Google 账号并完成验证",
                reasons=[detail, "已打开：%s" % action],
                data={"version": st["version"], "account_page": action},
            )

        if reinstall_if_blocked and st["version"]:
            # 44.x 装得上但一开就退，重装同版本没用；只有换包才有意义。
            # 这里不自动猜版本，只把结论说清楚。
            return Outcome(
                False, "blocked",
                "商店被系统拦下，且打不开账号页",
                reasons=[detail,
                         "当前版本 %s。版本可用性随 ONYX 的拦截策略变："
                         "V1 时期 42.0.18-31 可用、44.6.15 一开就退" % st["version"]],
                data={"version": st["version"]},
            )

        return Outcome(False, "failed", detail, data={"version": st["version"]})
