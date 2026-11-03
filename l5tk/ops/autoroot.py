"""一键 root：按「代价从低到高」逐条试，第一个成功就停。

六条路径，顺序不能换：

  0. 已经有 root        → 直接返回，一个字节都不动（幂等）
  1. adb root           → userdebug 上就够，adbd 重启，不碰分区
  2. 内核 exploit        → 拿临时 root，是打破「没 root 装不了 Magisk」的唯一入口
  3. Magisk 设备内补丁   → 已经有 su 时最快；备份 → patch → dd 写回
  4. fastbootd          → 不需要 su；Android 里的 userspace 写入，不经 abl
  5. fastboot flash     → ONYX 上被裁，留着给别的机器和万一被补回来的固件

为什么 4 排在 5 前面：ONYX 的 abl 把 bootloader 模式下的 `flash` handler
裁了（实测 `remote: 'unknown command'`），而 fastbootd 的写入是
OpenPartition + write(blockdev)，走另一套代码，裁前者不影响后者。
所以同样「给了镜像」，fastbootd 的成功率比 bootloader 模式高得多。

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
from .exploit import ExploitOps
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
            allow_fastboot: bool = True, force_kernel: bool = False,
            allow_exploit: bool = True, enter_seconds: int = 30) -> AutoResult:
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
                    "2) 内核 exploit（内核匹配才行；拿临时 root，重启失效）",
                    "3) Magisk 设备内补丁 → dd 写回（要有 su；把临时 root 变持久）",
                    "4) fastbootd 写入 %s（不需 su；绕开被裁的 abl）"
                    % (image or "<未给镜像>"),
                    "5) fastboot flash %s（要有镜像且未被裁）" % (image or "<未给镜像>")]
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

        # 2. 内核 exploit —— 裸机上唯一能打破「没 root 装不了 Magisk、
        #    装 Magisk 又要先有 root」这个循环的入口。它给的 root 是临时的，
        #    所以成功后不返回，继续往 Magisk 走，把它变持久。
        tmp_root = False
        if allow_exploit:
            try:
                eo = ExploitOps(self.adb, self.log).run_once(force=force_kernel)
                steps.append(Step("exploit", tried=True, ok=eo.ok,
                                  detail=self._detail(eo)))
                tmp_root = eo.ok
            except L5Error as e:
                steps.append(Step("exploit", tried=True, ok=False, detail=e.message))
        else:
            steps.append(Step("exploit", tried=False, ok=False,
                              detail="跳过：--no-exploit"))

        # 3. Magisk 设备内补丁（需要已有 su）
        su_ok, su_why = self.adb.su_probe()
        if su_ok:
            try:
                o = MagiskOps(self.adb, self.log).install_full(
                    part=part, with_module=with_module, reboot=reboot)
                steps.append(Step("magisk", tried=True, ok=o.ok, detail=self._detail(o)))
                if o.ok:
                    how = "exploit+magisk" if tmp_root else "magisk_dd"
                    return AutoResult(True, how, o.detail, steps,
                                      {"how": how, **o.data})
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

        # exploit 拿到了临时 root 但没能持久化：这不是失败，但必须说清后果。
        # 不过别在这里就收工 —— 手上还有临时 root 时设备是活的，
        # 后面 fastbootd 那条路也许还能把持久化补上。
        tmp_only = bool(tmp_root and not su_ok)

        # 4. fastbootd —— 不需要 su，也不需要 bootloader 支持 flash。
        #    ONYX 的 abl 裁的是 bootloader 模式下的 flash handler；
        #    fastbootd 是 Android 里的 userspace 进程，写分区走
        #    OpenPartition + write(blockdev)，两套实现，裁前者不影响后者。
        if image and allow_fastboot:
            try:
                profile, score = self._profile()
                # enter_seconds 有意压到 30：一键流程里让用户干等 90 秒
                # 才说一句「进不去 fastbootd」，体验上等于卡死。30 秒进不去
                # 的机器，基本就是不支持。
                # backup=True：fastbootd 进去之前设备还在 Android 里，有 root
                # 就能 dd 出原分区。没 root 时 backup_boot 会抛 DeviceError，
                # 这一步就记为失败 —— 宁可这条路不通，也不写一个没有退路的 boot。
                o = self.root.flash_boot(image, profile, part=part,
                                         backup=True, force=False, via="fastbootd",
                                         enter_seconds=enter_seconds)
                steps.append(Step("fastbootd", tried=True, ok=o.ok,
                                  detail=self._detail(o)))
                if o.ok:
                    return AutoResult(True, "fastbootd", self._detail(o), steps,
                                      {"how": "fastbootd", "profile_score": score,
                                       "backup": "none"})
            except L5Error as e:
                steps.append(Step("fastbootd", tried=True, ok=False, detail=e.message))
        else:
            steps.append(Step("fastbootd", tried=False, ok=False,
                              detail="跳过：%s" % ("没给镜像路径" if not image
                                                else "--no-fastboot")))

        # 5. fastboot 兜底 —— 只有给了镜像才动
        if image and allow_fastboot and self._fastboot_online():
            try:
                profile, score = self._profile()
                # backup=True：备份在「重启进 bootloader」之前做，那时设备还在
                # Android 里。没 root 就备份不出来 —— backup_boot 抛错，这一步
                # 记失败。以前这里传 False 并想着「大不了在 detail 里说明」，
                # 但刷坏的 boot 不会因为日志里提了一句就变得可救。
                o = self.root.flash_boot(image, profile, part=part,
                                         backup=True, force=False, via="fastboot")
                steps.append(Step("fastboot", tried=True, ok=o.ok,
                                  detail=self._detail(o)))
                if o.ok:
                    return AutoResult(True, "fastboot",
                                      self._detail(o),
                                      steps, {"how": "fastboot",
                                              "profile_score": score,
                                              "backup": "done"})
            except L5Error as e:
                steps.append(Step("fastboot", tried=True, ok=False, detail=e.message))
        elif image:
            steps.append(Step("fastboot", tried=False, ok=False,
                              detail="跳过：fastboot 上没有设备（或未授权 --no-fastboot）"))
        else:
            steps.append(Step("fastboot", tried=False, ok=False,
                              detail="跳过：没给镜像路径"))

        # 走到这里说明持久化没成：临时 root 是「能用一会儿」，
        # 必须如实说成这个，而不是打勾说 root 成功。
        if tmp_only:
            return AutoResult(
                True, "exploit_temp",
                "已拿到临时 root（uid=0），但 Magisk 没装上 —— 重启后失效",
                steps, {"how": "exploit", "persist": False})

        # 哪条都没通：把「为什么」写成能照做的几行
        why = ["六条路径都不成立："]
        if locked == "1":
            why.append("  bootloader 还是锁着的（flash.locked=1），写 boot 会被拒；"
                       "先 `fastboot flashing unlock`（会清数据）")
        if not su_ok:
            why.append("  没有 su：dd 读写 boot 分区的前提不成立")
        why.append("  给一个已补丁的 boot 镜像再跑一次："
                   "l5tk root auto --image magisk_patched.img")
        why.append("  镜像可以从官方固件包里取（不用自己备份）："
                   "l5tk root payload --ota update.zip")
        why.append("  内核版号不同的机器只有内核 exploit 一条路，"
                   "确认要试加 --force-kernel（跑错偏移会 kernel panic）")
        detail = "\n".join(why)
        return AutoResult(False, "no_entry", detail, steps, {"how": "none"})
