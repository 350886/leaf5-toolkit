"""一键 root：按「代价从低到高」逐条试，第一个成功就停。

四条路径，顺序不能换：

  0. 已经有 root        → 直接返回，一个字节都不动（幂等）
  1. adb root           → userdebug 上就够，adbd 重启，不碰分区
  2. Magisk 设备内补丁   → 已经有 su 时最快；备份 → patch → dd 写回
  3. fastboot flash     → 只有在 1/2 都不通、且给了镜像时才试

为什么第 2 条排在 fastboot 前面：Leaf5 的 abl 把 `fastboot flash` 裁了
（实测 `remote: 'unknown command'`），而 dd 需要 su。所以「有 su」的机器
走 dd 反而更可靠；「没 su 也没解锁」的机器哪条都不通，此时不能假装成功。

一键不等于强行成功：哪条不通就报哪条的原因，并把下一步写清楚。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..core.adb import Adb
from ..core.errors import L5Error, Refused
from ..core.fastboot import Fastboot
from ..core.result import Outcome
from ..device import by_key, detect
from ..logx import Logger
from .magisk import MagiskOps
from .root import RootOps


@dataclass
class Step:
    name: str
    tried: bool = False
    ok: bool = False
    detail: str = ""


@dataclass
class AutoResult:
    ok: bool
    stage: str
    detail: str
    steps: list[Step] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    def to_outcome(self) -> Outcome:
        return Outcome(self.ok, self.stage, self.detail,
                       reasons=["%s %s" % ("✓" if s.ok else ("✗" if s.tried else "·"),
                                           s.detail) for s in self.steps],
                       data=self.data)


class AutoRoot:
    def __init__(self, adb: Adb, fb: Fastboot, log: Logger):
        self.adb = adb
        self.fb = fb
        self.log = log
        self.root = RootOps(adb, fb, log)

    @staticmethod
    def _detail(o: Outcome) -> str:
        """Outcome.reasons 里放的是真正的原因（remote 错误、errno），
        只取 detail 会变成一句没信息量的「刷入失败」。"""
        if not o.reasons:
            return o.detail
        return o.detail + "\n" + "\n".join("  " + r for r in o.reasons)

    # ---------------- 各条路径 ----------------
    def _fastboot_online(self) -> bool:
        try:
            return bool(self.fb.devices())
        except L5Error:
            return False

    def _profile(self):
        det = detect(self.adb.props())
        self.log.info("机型：%s（匹配分 %d）" % (det.profile.label, det.score))
        return det.profile, det.score

    # ---------------- 主流程 ----------------
    def run(self, part: str | None = None, with_module: bool = False,
            reboot: bool = True, image: str | Path | None = None,
            allow_fastboot: bool = True) -> AutoResult:
        steps: list[Step] = []

        # 0. 已经 root 就别再走一遍 —— 重复 dd 写 boot 是唯一能把好机器搞砖的操作
        st = self.root.status()
        if st.ok:
            steps.append(Step("already", tried=True, ok=True,
                              detail="已有 root（%s）" % (st.data.get("magisk") or "su")))
            return AutoResult(True, "already_rooted", st.detail, steps,
                              {"how": "none", **st.data})

        locked = (st.data.get("flash_locked") or "").strip()
        steps.append(Step("status", tried=True, ok=False,
                          detail="当前无 root；flash.locked=%s" % (locked or "?")))

        # dry-run 只列计划：adb root 会重启 adbd、magisk 会 dd 写块设备，
        # 两个都是实质动作，不能因为「只想看看」就真做。
        if self.adb.cfg.dry_run:
            plan = ["1) adb root（userdebug 才可能成功）",
                    "2) Magisk 设备内补丁 → dd 写回（要有 su）",
                    "3) fastboot flash %s（要有镜像且未被裁）" % (image or "<未给镜像>")]
            steps.append(Step("plan", tried=False, ok=False,
                              detail="将依次尝试：\n     " + "\n     ".join(plan)))
            return AutoResult(True, "dry_run",
                              "dry-run：设备未改动。去掉 --dry-run 才真执行",
                              steps, {"how": "none"})

        # 1. adb root —— userdebug 上一步到位，且不动分区
        if st.data.get("can_try_adb_root"):
            o = self.root.try_adb_root()
            steps.append(Step("adb_root", tried=True, ok=o.ok, detail=o.detail))
            if o.ok:
                return AutoResult(True, "adb_root", o.detail, steps,
                                  {"how": "adb_root"})
        else:
            steps.append(Step("adb_root", tried=False, ok=False,
                              detail="跳过：ro.build.type=%s，adb root 会拒绝"
                                     % (self.adb.getprop("ro.build.type") or "?")))

        # 2. Magisk 设备内补丁（需要已有 su）
        su_ok, su_why = self.adb.su_probe()
        if su_ok:
            try:
                o = MagiskOps(self.adb, self.log).install_full(
                    part=part, with_module=with_module, reboot=reboot)
                steps.append(Step("magisk", tried=True, ok=o.ok, detail=self._detail(o)))
                if o.ok:
                    return AutoResult(True, "magisk", o.detail, steps,
                                      {"how": "magisk_dd", **o.data})
                # 失败继续往下试 fastboot，但把失败原因留在 steps 里
            except Refused as e:
                steps.append(Step("magisk", tried=True, ok=False,
                                  detail="dry-run 拦下：%s" % e.message))
                return AutoResult(False, "dry_run", e.message, steps, {"how": "none"})
            except L5Error as e:
                steps.append(Step("magisk", tried=True, ok=False, detail=e.message))
        else:
            steps.append(Step("magisk", tried=False, ok=False,
                              detail="跳过：没有 su（%s）" % (su_why or "未知")))

        # 3. fastboot 兜底 —— 只有给了镜像才动
        if image and allow_fastboot and self._fastboot_online():
            try:
                profile, score = self._profile()
                # backup=False 是有意的：没 root 的机器 dd 读不了块设备，
                # 这条路本来就没有"刷前备份"的能力。诚实的做法是把这件事
                # 写在 detail 里，而不是先假装备份、再在 backup_boot 里炸掉。
                o = self.root.flash_boot(image, profile, part=part,
                                         backup=False, force=False)
                steps.append(Step("fastboot", tried=True, ok=o.ok,
                                  detail=self._detail(o)))
                if o.ok:
                    return AutoResult(True, "fastboot",
                                      self._detail(o) + "\n注意：未做刷前备份（无 root 时无法 dd）",
                                      steps, {"how": "fastboot",
                                              "profile_score": score,
                                              "backup": "none"})
            except L5Error as e:
                steps.append(Step("fastboot", tried=True, ok=False, detail=e.message))
        elif image:
            steps.append(Step("fastboot", tried=False, ok=False,
                              detail="跳过：fastboot 上没有设备（或未授权 --no-fastboot）"))
        else:
            steps.append(Step("fastboot", tried=False, ok=False,
                              detail="跳过：没给镜像路径"))

        # 哪条都没通：把「为什么」写成能照做的三行
        why = ["四条路径都不成立："]
        if locked == "1":
            why.append("  bootloader 还是锁着的（flash.locked=1），写 boot 会被拒；"
                       "先 `fastboot flashing unlock`（会清数据）")
        if not su_ok:
            why.append("  没有 su：dd 读写 boot 分区的前提不成立")
        why.append("  给一个已补丁的 boot 镜像再跑一次："
                   "l5tk root auto --image magisk_patched.img")
        detail = "\n".join(why)
        return AutoResult(False, "no_entry", detail, steps, {"how": "none"})
