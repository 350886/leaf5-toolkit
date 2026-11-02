"""fastbootd（userspace fastboot）通路 —— 绕开被裁掉 flash 的 abl。

为什么这条路成立：
  `fastboot flash` 在 bootloader 模式下由 abl 的命令分发表处理，ONYX 把
  这条 handler 删了，所以刷任何分区都回 `remote: 'unknown command'`。
  而 fastbootd 是跑在 Android 里的 userspace 守护进程
  （system/core/fastbootd + libfastboot），写入走
  `OpenPartition -> write(blockdev)`，根本不经过 abl 的分发表 ——
  abl 只负责把 fastbootd 拉起来，那是 `reboot fastboot`（bootloader 命令），
  和 flash 不是一个 handler，裁 flash 不会连带把这条也裁掉。

  另一条依据：动态分区（super）设备必然带 fastbootd。system / product /
  vendor 这些逻辑分区在 bootloader 里根本不存在，只能由 fastbootd
  解开 super 再写。ONYX 4.2 是 Android 13 + 动态分区，所以 fastbootd 一定在。

判定不靠猜：唯一可靠的标志是 `fastboot getvar is-userspace` 返回 yes。
bootloader 模式给 no（或直接空），fastbootd 给 yes。别的特征
（比如 product 名字、getvar all 的条目数）两边会重样，不能用来判。

这条不通的时候不要硬顶 —— 退回 EDL（ops/edl.py），那是唯一不依赖
任何 Android / bootloader 组件的路。
"""

from __future__ import annotations

import time

from ..core.adb import Adb
from ..core.errors import FastbootError, L5Error
from ..core.fastboot import Fastboot
from ..core.result import ExecResult
from ..logx import Logger

USERSPACE_YES = ("yes", "true", "1")

# `adb reboot fastboot` 是 Android 10 起的写法，更早的叫 fastbootd。
# 两个都试：ONYX 的 4.2 是 13，但万一 adbd 被裁成只认老名字。
ADB_ENTER_TARGETS = ("fastboot", "fastbootd")


def _one(text: str, n: int = 200) -> str:
    """多行输出压成一行 —— 报错信息只需要里面那句真正的错。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:n]
    return "（无输出）"


class Fastbootd:
    """userspace fastboot 的操作面：进、刷、退。"""

    def __init__(self, adb: Adb, fb: Fastboot, log: Logger):
        self.adb = adb
        self.fb = fb
        self.log = log

    # ---------------- 状态 ----------------
    def devices(self) -> list[str]:
        try:
            return self.fb.devices()
        except L5Error:
            return []

    def is_userspace(self) -> bool:
        """当前 fastboot 是不是 fastbootd。读不到就当不是（保守）。"""
        try:
            v = (self.fb.getvar("is-userspace") or "").strip().lower()
        except L5Error:
            return False
        return v in USERSPACE_YES

    def mode(self) -> str:
        """'fastbootd' / 'bootloader' / 'none'。"""
        if not self.devices():
            return "none"
        return "fastbootd" if self.is_userspace() else "bootloader"

    def partition_size(self, part: str) -> int:
        """fastbootd 只读得到分区容量，读不到内容 —— 这是它唯一的校验手段。"""
        for key in ("partition-size:" + part, "partition-size:%s" % part):
            try:
                raw = (self.fb.getvar(key) or "").strip()
            except L5Error:
                continue
            try:
                return int(raw, 16) if not raw.isdigit() else int(raw)
            except ValueError:
                continue
        return 0

    # ---------------- 进去 ----------------
    def _wait(self, seconds: int, poll: float = 1.5) -> bool:
        """等 fastbootd 上线。只认 is-userspace=yes，不等 bootloader。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.devices() and self.is_userspace():
                return True
            time.sleep(max(0.2, poll))
        return False

    def enter(self, seconds: int = 90) -> tuple[bool, str]:
        """把设备弄进 fastbootd。返回 (成功, 怎么进去的 / 为什么不行)。"""
        m = self.mode()
        if m == "fastbootd":
            return True, "已经在 fastbootd（is-userspace=yes）"

        # 1) 已经在 bootloader：让 bootloader 自己切。
        #    `reboot fastboot` 和 `flash` 是两个 handler，裁后者不影响前者。
        if m == "bootloader":
            self.log.info("设备在 bootloader，切 fastbootd…")
            r = self.fb.run("reboot", "fastboot", timeout=self.fb.cfg.timeout_short)
            if not r.ok:
                return False, (
                    "bootloader 拒绝 `reboot fastboot`：%s" % _one(r.text))
            if self._wait(seconds):
                return True, "bootloader → `reboot fastboot`"
            return False, ("发了 `reboot fastboot` 但 %d 秒内没等到 "
                           "is-userspace=yes（设备可能不支持 fastbootd，"
                           "或在重启途中掉线）" % seconds)

        # 2) 没有 fastboot 设备：看 adb。这条是主路 —— 工具通常在系统里
        #    就对 adb 有连接，直接一条 reboot 比让用户手按音量键靠谱。
        try:
            devs = self.adb.devices()
        except L5Error:
            devs = []
        if not devs:
            return False, ("adb 与 fastboot 都没有设备；插 USB 并确认"
                           "「USB 调试」已开")

        dyn = (self.adb.getprop("ro.boot.dynamic_partitions") or "").strip()
        if dyn and dyn not in ("true", "1"):
            # 没有动态分区大概率没有 fastbootd，但仍然试一次：
            # 有些厂商手动塞了 fastbootd 进去。
            self.log.warn("ro.boot.dynamic_partitions=%s，fastbootd 可能不存在，仍然试一次" % dyn)

        last = ""
        for target in ADB_ENTER_TARGETS:
            self.log.info("adb reboot %s" % target)
            r = self.adb.run("reboot", target, timeout=self.adb.cfg.timeout_short)
            if not r.ok:
                last = "adb reboot %s 失败：%s" % (target, _one(r.text))
                continue
            if self._wait(seconds):
                return True, "adb reboot %s" % target
            last = ("adb reboot %s 之后 %d 秒内 fastboot 没报 is-userspace=yes"
                    % (target, seconds))
        return False, last

    # ---------------- 刷 ----------------
    def flash(self, part: str, image: str, timeout: int | None = None
              ) -> tuple[ExecResult, str]:
        """刷一个分区，返回 (结果, 真正用的分区名)。

        分区名回退：fastbootd 里逻辑分区不带 _a/_b 后缀，物理分区带。
        boot 是物理分区，所以 `boot_a` 在 fastbootd 里也应该在；
        但保险起见 unknown partition 时去掉后缀再试一次 ——
        比让整条通路在这里断掉强。
        """
        if self.mode() != "fastbootd":
            raise FastbootError(
                "当前不是 fastbootd（is-userspace≠yes），拒绝 flash",
                hint="先调 enter() 切过去；在 bootloader 里刷必然被 abl 拒")
        r = self.fb.flash(part, image, timeout=timeout)
        if r.ok or "_" not in part:
            return r, part

        alt = part.rsplit("_", 1)[0]
        if "unknown partition" not in r.text.lower() and "not found" not in r.text.lower():
            return r, part
        self.log.warn("fastbootd 不认识 %s，改试 %s" % (part, alt))
        r2 = self.fb.flash(alt, image, timeout=timeout)
        return r2, alt if r2.ok else part

    def reboot(self, target: str = "") -> ExecResult:
        """"刷完回系统。fastbootd 下 reboot 是正常命令，不会被裁。"""
        try:
            return self.fb.reboot(target)
        except L5Error:
            # 有的 fastbootd 版本对不带参数的 reboot 挑食，退回 adb。
            return self.adb.run("reboot", *([target] if target else []),
                                timeout=self.adb.cfg.timeout_short)

    def __repr__(self) -> str:
        return "<Fastbootd mode=%s>" % self.mode()
