"""Root：状态判定 + 刷已补丁的 boot 镜像。

三条写 boot 的路线，按「代价从低到高」排：

  * `root magisk --full`（ops/magisk.py）：已经 root 时用。设备内跑
    boot_patch.sh 生成 new-boot.img，再 dd 写回。
  * `root flash`（本模块）：手上已经有打好补丁的镜像时用。via=auto 时按
    dd → fastbootd → fastboot 依次选，理由见 flash_boot 的 docstring。
  * `root edl`（ops/edl.py）：9008 直写，不依赖 Android 与 bootloader。
    需要与 SoC 匹配的 firehose programmer —— Leaf5 是 LITO(SM6350)，
    Tab 系列是 BENGAL(SM6115)，两份 lito programmer 在 Tab 上不认。

为什么要 fastbootd：ONYX 的 abl 把 bootloader 模式下的 `flash` handler
裁了，`fastboot flash boot_b` 实测返回 `remote: 'unknown command'`。
但 fastbootd 是 Android 里的 userspace 守护进程，写分区走
OpenPartition + write(blockdev)，根本不经过 abl 的分发表。

每一步都留证据，刷之前尽量能备份；备份不了就在结果里明说「未备份」，
而不是先假装备份、再在刷坏后没有退路。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from ..core.adb import Adb
from ..core.errors import DeviceError, ImageError, L5Error, Refused
from ..core.fastboot import Fastboot, list_boot_images
from ..core.result import Outcome
from ..device.models import Profile
from ..logx import Logger
from .probe import detect_root

BOOT_MAGIC = b"ANDROID!"
HEADER_MIN = 1632


@dataclass
class BootImage:
    path: Path
    size: int
    page_size: int
    kernel_size: int
    ramdisk_size: int
    header_version: int
    os_version: str
    name: str

    @property
    def expect_size(self) -> int:
        """按 header 推算的最小合法大小（页对齐）。"""
        p = self.page_size

        def align(n: int) -> int:
            return (n + p - 1) // p * p

        return p + align(self.kernel_size) + align(self.ramdisk_size)

    def describe(self) -> str:
        return ("%s  %.1f MB  page=%d  kernel=%.1f MB  ramdisk=%.1f MB  "
                "header=v%d  os=%s" % (
                    self.path.name, self.size / 1048576, self.page_size,
                    self.kernel_size / 1048576, self.ramdisk_size / 1048576,
                    self.header_version, self.os_version or "?"))


def parse_boot(path: str | Path) -> BootImage:
    """解析 Android boot image header。

    只解析到能判真伪就够了：magic、page_size、kernel/ramdisk 长度、header 版本。
    故意不解析 v3/v4 的 vendor_boot —— 那是另一套结构，混在一起容易把
    「不是 boot 镜像」误判成「镜像损坏」。
    """
    p = Path(path)
    if not p.is_file():
        raise ImageError("镜像不存在：%s" % p)
    size = p.stat().st_size
    with open(p, "rb") as f:
        head = f.read(HEADER_MIN)
    if len(head) < 64:
        raise ImageError("文件只有 %d 字节，不像 boot 镜像" % len(head))
    if head[:8] != BOOT_MAGIC:
        got = head[:8]
        raise ImageError(
            "magic 不是 ANDROID!（读到 %r）" % got,
            hint="这是 recovery/vendor_boot/普通文件，刷进 boot 分区会砖",
        )
    kernel_size, = struct.unpack_from("<I", head, 8)
    ramdisk_size, = struct.unpack_from("<I", head, 16)
    page_size, = struct.unpack_from("<I", head, 36)
    header_version, = struct.unpack_from("<I", head, 40)
    os_raw, = struct.unpack_from("<I", head, 44)
    name = head[48:64].split(b"\x00")[0].decode("ascii", "replace")

    if page_size not in (2048, 4096, 8192, 16384):
        raise ImageError("page_size=%d 不合理，文件可能被截断或加密" % page_size)
    if kernel_size <= 0 or ramdisk_size <= 0:
        raise ImageError("kernel/ramdisk 长度为 0，不是可启动镜像")

    img = BootImage(p, size, page_size, kernel_size, ramdisk_size,
                    header_version, _os_str(os_raw), name)
    if size < img.expect_size:
        raise ImageError(
            "文件被截断：实际 %d 字节，header 要求至少 %d" % (size, img.expect_size))
    return img


def _os_str(v: int) -> str:
    a = (v >> 14) & 0x7F
    b = (v >> 7) & 0x7F
    c = v & 0x7F
    if not a and not b and not c:
        return ""
    return "%d.%d.%d" % (a, b, c)


class RootOps:
    def __init__(self, adb: Adb, fb: Fastboot, log: Logger):
        self.adb = adb
        self.fb = fb
        self.log = log
        # fastbootd 只在真要写 boot 时才用到，构造它不碰设备（不发命令）。
        from .fastbootd import Fastbootd
        self.fbd = Fastbootd(adb, fb, log)

    # ---------------- 分区判定 ----------------
    def by_name(self) -> set[str]:
        """读设备的分区表。这是硬证据，比 profile 里写死的 ab_slot 可靠。"""
        r = self.adb.sh("ls", "/dev/block/by-name/", timeout=self.adb.cfg.timeout_short)
        if not r.ok:
            return set()
        return {n.strip() for n in r.out.replace("\r", " ").split() if n.strip()}

    def resolve_partition(self, profile: Profile, part: str | None = None
                          ) -> tuple[str, str]:
        """定下要刷哪个分区，返回 (分区名, 依据)。

        顺序：设备分区表 > profile 的 ab_slot。
        实测 Leaf5 有 boot_a / boot_b，当前槽位 b —— 若按「单槽 boot」去刷，
        fastboot 会报找不到分区，或者更糟：刷到别的设备的 boot。
        """
        base = part or profile.boot_part
        names = self.by_name()
        slot = (self.adb.getprop("ro.boot.slot_suffix") or "").lstrip("_")

        if not names:
            # 读不到分区表（未 root 且受限）：退回 profile，但要说清楚
            if profile.ab_slot:
                if not slot:
                    raise DeviceError(
                        "读不到分区表，也读不到当前槽位，不敢定分区",
                        hint="用 --part boot_a / boot_b 明确指定")
                return "%s_%s" % (base, slot), "分区表不可读，按 profile 的 A/B + 槽位 %s" % slot
            return base, "分区表不可读，按 profile 的单槽"

        if base in names:
            return base, "分区表里有 %s（单槽）" % base

        if slot and "%s_%s" % (base, slot) in names:
            return "%s_%s" % (base, slot), "A/B 设备，当前槽位 %s" % slot

        ab = [s for s in ("a", "b") if "%s_%s" % (base, s) in names]
        if ab:
            raise DeviceError(
                "设备是 A/B（有 %s），但读不到当前槽位"
                % "/".join("%s_%s" % (base, s) for s in ab),
                hint="用 --part %s_%s 明确指定槽位" % (base, ab[0]),
            )
        raise DeviceError(
            "分区表里没有 %s，也没有 %s_a/b" % (base, base),
            hint="这台机器的 boot 分区可能叫别的名字；先 `l5tk probe --raw` 看一眼",
        )

    # ---------------- 状态 ----------------
    def status(self) -> Outcome:
        # 只读检查，不自动 `adb root` —— 那条命令会让 adbd 重启、当前连接断掉，
        # 放在「看一眼状态」里代价太大。要试另有 `root adb`。
        su, su_detail, magisk, _adb_root = detect_root(self.adb, allow_adb_root=False)
        locked = self.adb.getprop("ro.boot.flash.locked")
        debuggable = self.adb.getprop("ro.debuggable").strip() == "1"
        build_type = self.adb.getprop("ro.build.type").strip()
        can_adb_root = debuggable and build_type != "user"
        state = "rooted" if (su or magisk) else "none"
        if state == "rooted":
            detail = "已有 root（%s）" % (magisk or "su")
        elif su_detail:
            detail = "当前没有 root 权限：%s" % su_detail
        elif can_adb_root:
            detail = "当前没有 root 权限；这台是 userdebug，可以试 adb root"
        else:
            detail = "当前没有 root 权限；user 版只能 Magisk 补 boot 再刷"
        return Outcome(
            ok=(state != "none"), stage=state, detail=detail,
            data={"su": su, "magisk": magisk, "adb_root": False,
                  "can_try_adb_root": can_adb_root,
                  "su_detail": su_detail,
                  "flash_locked": locked,
                  "unlocked": locked.strip() == "0"},
        )

    def try_adb_root(self) -> Outcome:
        """userdebug 上这一步就够了，且不动任何分区 —— 优先试它。"""
        if self.adb.adb_root():
            ok = self.adb.su_available() or self.adb.sh_out("id") .startswith("uid=0")
            return Outcome(ok, "adb_root",
                           "adb root 成功，当前 shell 已是 root" if ok else
                           "adb root 返回成功但 id 不是 uid=0",
                           data={"su": ok})
        return Outcome(False, "none", "adb root 不可用（user 版构建会拒绝）",
                       reasons=["userdebug/user 的区别在 ro.build.type"])

    # ---------------- 备份 ----------------
    def backup_boot(self, part: str = "boot", out_dir: Path | None = None) -> Path:
        """dd 出当前 boot 分区并 pull 回来。必须已 root，否则 dd 读不到块设备。"""
        if not (self.adb.su_available() or self.adb.adb_root()):
            raise DeviceError(
                "没法备份 boot：当前没有 root",
                hint="先 adb root（userdebug）或用 Magisk 拿到 root；"
                     "不备份就刷，出错没有回滚的底片",
            )
        out_dir = Path(out_dir or self.adb.cfg.work_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        remote = "/data/local/tmp/boot_backup.img"
        blk = "/dev/block/by-name/%s" % part
        r = self.adb.sh("su", "-c", "dd if=%s of=%s bs=4096" % (blk, remote), timeout=120)
        if not r.ok:
            raise DeviceError("dd 备份失败", hint=r.text[:300])
        local = out_dir / ("%s_backup_%d.img" % (part, int(__import__("time").time())))
        p = self.adb.pull(remote, str(local))
        if not p.ok:
            raise DeviceError("拉取备份失败", hint=p.text[:300])
        self.adb.sh("su", "-c", "rm -f %s" % remote, timeout=15)
        if local.stat().st_size < 1024 * 1024:
            raise DeviceError("备份文件只有 %d 字节，明显不对" % local.stat().st_size)
        self.log.ok("已备份 %s → %s（%.1f MB）"
                    % (part, local, local.stat().st_size / 1048576))
        return local

    # ---------------- 刷入 ----------------
    # ---------------- 刷入 ----------------
    def flash_boot(self, image: str | Path, profile: Profile,
                   part: str | None = None, backup: bool = True,
                   force: bool = False, timeout: int | None = None,
                   wait_seconds: int = 180, via: str = "auto",
                   enter_seconds: int = 90) -> Outcome:
        """把已打补丁的 boot 镜像写进设备。

        via 决定走哪条通路，三条的代价与前提完全不同：

          dd          有 su 就能走，不重启，写完立刻能读回校验。首选。
          fastbootd   Android 里的 userspace fastboot。abl 把 bootloader
                      模式的 flash handler 裁了，但 fastbootd 的写入走
                      OpenPartition + write(blockdev)，不经过 abl 的分发表，
                      所以这条路在 ONYX 上仍然成立。
          fastboot    传统 bootloader 模式。ONYX 上会回 unknown command，
                      留着它是为了别的机器、以及万一新固件把 handler 补回来。

        auto 的顺序是 dd → fastbootd → fastboot：从「不用重启」排到
        「要重启且可能被拒」，能少动一次设备就少动一次。
        """
        img = parse_boot(image)
        self.log.info("镜像校验通过：" + img.describe())

        # dry-run 必须挡在所有真动作之前：备份要 dd 块设备并 pull，
        # 刷入前还要重启 —— 这些都不是「只检查不动手」。
        if self.adb.cfg.dry_run:
            raise Refused("dry-run：镜像已校验，备份/重启/刷入均未执行")

        if not profile.verified and not force:
            raise DeviceError(
                "机型 %s 的档案没在真机验证过，拒绝刷入" % profile.label,
                hint="确认要刷就加 --force；刷错 boot 分区会导致设备无法开机",
            )

        partition, why = self.resolve_partition(profile, part)
        self.log.info("目标分区：%s（%s）" % (partition, why))

        plan = self.plan_via(via)
        self.log.info("写入通路：%s" % plan)

        if plan == "dd":
            return self._flash_dd(partition, img, backup=backup,
                                  wait_seconds=wait_seconds)
        if plan == "fastbootd":
            return self._flash_fastbootd(partition, img, backup=backup,
                                         timeout=timeout, wait_seconds=wait_seconds,
                                         enter_seconds=enter_seconds)
        return self._flash_fastboot(partition, img, profile, backup=backup,
                                    force=force, timeout=timeout,
                                    wait_seconds=wait_seconds)

    def plan_via(self, via: str = "auto") -> str:
        """把 auto 解析成具体通路。显式指定的直接照办，失败由那条路自己报。"""
        v = (via or "auto").strip().lower()
        allowed = ("auto", "dd", "fastbootd", "fastboot")
        if v not in allowed:
            raise DeviceError("不认识的写入通路 %r" % via,
                              hint="可选：" + " / ".join(allowed))
        if v != "auto":
            return v

        try:
            su_ok, _why = self.adb.su_probe()
        except L5Error:
            su_ok = False
        if su_ok:
            return "dd"

        # 没 su：fastbootd / fastboot 都要先能看见设备。这里不探「能不能
        # 进 fastbootd」—— 那要真发命令，放在 _flash_fastbootd 里报。
        try:
            adb_ok = bool(self.adb.devices())
        except L5Error:
            adb_ok = False
        if adb_ok or self.fbd.devices():
            return "fastbootd"
        raise DeviceError(
            "adb 与 fastboot 都没有设备",
            hint="插 USB、开「USB 调试」；已在 fastboot 模式就确认驱动装好了")

    def _part_size(self, dev: str) -> int:
        """分区容量（字节）。读不到返回 0 —— 0 表示「不知道」，不能当 0 容量用。"""
        r = self.adb.sh("blockdev", "--getsize64", dev, root=True)
        if not r.ok:
            return 0
        try:
            return int(r.out.strip())
        except ValueError:
            return 0

    def _flash_dd(self, partition: str, img: BootImage, backup: bool,
                  wait_seconds: int) -> Outcome:
        """有 su 时直接 dd 写块设备。

        dd 没有 fastboot 那层「整块写入」的原子性，写一半掉电就是半个镜像，
        所以备份与写完回读 md5 这两件事一个都不能省。
        """
        dev = "/dev/block/by-name/" + partition
        size = img.size
        cap = self._part_size(dev)
        if cap and size > cap:
            raise DeviceError(
                "镜像 %d 字节 > 分区 %s 的 %d 字节" % (size, partition, cap),
                hint="刷进去会被截断；确认镜像与机型 / 槽位对得上")
        if cap:
            self.log.info("分区容量 %.1f MB，镜像 %.1f MB"
                          % (cap / 1048576.0, size / 1048576.0))

        backup_path = None
        if backup:
            backup_path = self.backup_boot(partition)

        remote = "/data/local/tmp/l5tk_flash.img"
        self.log.info("推送镜像到设备…")
        p = self.adb.push(str(img.path), remote)
        if not p.ok:
            raise DeviceError("推送镜像失败", hint=p.text[:300])

        m1: list[str] = []
        try:
            self.log.info("写 %s…" % partition)
            w = self.adb.sh("dd", "if=" + remote, "of=" + dev, "bs=1M",
                            "conv=fsync", timeout=self.adb.cfg.timeout_flash,
                            root=True)
            if not w.ok:
                return self._rollback_dd(partition, backup_path,
                                         "dd 写回失败：" + (w.text[:200] or "无输出"))

            # 回读：只读前 size 字节。md5 必须等于推上去那份，
            # 不等就是没写全（分区尾部的旧数据不会影响前 size 字节）。
            m1 = self.adb.sh_out("md5sum", remote,
                                 timeout=self.adb.cfg.timeout_normal, root=True).split()
            blk = (size + 1048575) // 1048576
            m2 = self.adb.sh_out(
                "dd if=%s bs=1M count=%d 2>/dev/null | head -c %d | md5sum"
                % (dev, blk, size),
                timeout=self.adb.cfg.timeout_flash, root=True).split()
            if m1 and m2 and m1[0] != m2[0]:
                return self._rollback_dd(partition, backup_path,
                                         "回读 md5 不符（%s != %s）" % (m1[0], m2[0]))
            self.log.ok("回读 md5 一致 %s" % (m1[0] if m1 else "?"))
        finally:
            self.adb.sh("rm", "-f", remote, timeout=self.adb.cfg.timeout_short,
                        root=True)

        self.log.info("重启…")
        self.adb.sh("reboot", timeout=self.adb.cfg.timeout_short, root=True)
        back = self.fb.wait_for_adb(seconds=wait_seconds)
        return Outcome(
            True, "flashed_dd",
            "已 dd 写入 %s%s；%s" % (
                partition,
                "（已备份）" if backup_path else "（未备份）",
                "设备已重启并回到 adb" if back
                else "重启后 %d 秒内没回到 adb，可能卡在开机" % wait_seconds),
            data={"partition": partition, "via": "dd",
                  "backup": str(backup_path) if backup_path else "",
                  "image": str(img.path),
                  "md5": m1[0] if m1 else ""})

    def _rollback_dd(self, partition: str, backup_path, why: str) -> Outcome:
        """写坏了立刻把备份 dd 回去。没备份就如实报，绝不假装回滚过。"""
        if backup_path is None:
            return Outcome(
                False, "write_failed_no_backup",
                "写入失败，且没有备份可回滚：" + why,
                reasons=["先别重启 —— 设备上的 boot 现在是坏的",
                         "刷回原厂：l5tk root flash <原厂 boot.img> --via fastbootd",
                         "或 9008 直写：l5tk root edl --image <原厂 boot.img>"])
        self.log.warn("写入失败，用备份回滚：%s" % backup_path)
        remote = "/data/local/tmp/l5tk_rollback.img"
        p = self.adb.push(str(backup_path), remote)
        if not p.ok:
            return Outcome(False, "rollback_failed", "回滚推送失败：" + why,
                           reasons=[p.text[:200]])
        dev = "/dev/block/by-name/" + partition
        r = self.adb.sh("dd", "if=" + remote, "of=" + dev, "bs=1M", "conv=fsync",
                        timeout=self.adb.cfg.timeout_flash, root=True)
        self.adb.sh("rm", "-f", remote, timeout=self.adb.cfg.timeout_short, root=True)
        if not r.ok:
            return Outcome(False, "rollback_failed", "回滚也失败了：" + why,
                           reasons=[r.text[:200], "备份在 %s" % backup_path])
        return Outcome(False, "write_failed_rolled_back",
                       "写入失败，已用备份回滚：" + why,
                       data={"backup": str(backup_path)})

    def _flash_fastbootd(self, partition: str, img: BootImage, backup: bool,
                         timeout: int | None, wait_seconds: int,
                         enter_seconds: int = 90) -> Outcome:
        """fastbootd 通路。真正的理由写在 ops/fastbootd.py 顶部。"""
        # 备份要在进 fastbootd 之前做：进去以后 adb 就断了，
        # 而且 fastbootd 只能读分区容量、读不了内容。
        backup_path = None
        if backup:
            try:
                backup_path = self.backup_boot(partition)
            except L5Error as e:
                self.log.warn("刷前备份做不了：%s" % e.message)

        ok, how = self.fbd.enter(seconds=enter_seconds)
        if not ok:
            return Outcome(
                False, "no_fastbootd",
                "进不了 fastbootd：%s" % how,
                reasons=["fastbootd 进不去时，写 boot 只剩三条路：",
                         "  1) root exploit 拿临时 root → root magisk --full（dd 写）",
                         "  2) l5tk root edl --image <img>（9008 直写，要匹配的 programmer）",
                         "  3) 换回一条能 fastboot flash 的固件/机型"])
        self.log.info("已进 fastbootd（%s）" % how)

        cap = self.fbd.partition_size(partition)
        if cap and img.size > cap:
            return Outcome(False, "image_too_big",
                           "镜像 %d 字节 > 分区 %s 的 %d 字节"
                           % (img.size, partition, cap),
                           reasons=["刷进去会被截断，已中止"])

        self.log.info("fastbootd 写入 %s…" % partition)
        r, used = self.fbd.flash(partition, str(img.path), timeout=timeout)
        if not r.ok:
            detail = r.text.strip()[:300] or "无输出"
            if backup_path:
                self.log.warn("写入失败，用备份刷回")
                rb, _u = self.fbd.flash(partition, str(backup_path), timeout=timeout)
                if rb.ok:
                    self.fbd.reboot()
                    return Outcome(False, "flash_failed_rolled_back",
                                   "fastbootd 刷入失败，已刷回备份",
                                   reasons=[detail], data={"backup": str(backup_path)})
            return Outcome(
                False, "flash_failed", "fastbootd 刷入失败",
                reasons=[detail,
                         "常见原因：bootloader 还是锁的（fastbootd 会回 "
                         "Device is locked）；或这台机器的 fastbootd 也被裁了"],
                data={"backup": str(backup_path) if backup_path else "",
                      "via": "fastbootd"})

        self.fbd.reboot()
        self.log.info("等待设备回到 adb")
        back = self.fb.wait_for_adb(seconds=wait_seconds)
        return Outcome(
            True, "flashed_fastbootd",
            "fastbootd 已写入 %s%s；%s" % (
                used, "（已备份）" if backup_path else "（未备份）",
                "设备已重启并回到 adb" if back
                else "重启后 %d 秒内没回到 adb，可能卡在开机" % wait_seconds),
            data={"partition": used, "via": "fastbootd",
                  "backup": str(backup_path) if backup_path else "",
                  "image": str(img.path)})

    def _flash_fastboot(self, partition: str, img: BootImage, profile: Profile,
                        backup: bool, force: bool, timeout: int | None,
                        wait_seconds: int) -> Outcome:
        """传统 bootloader 模式。ONYX 上这一步会回 unknown command。"""
        locked = self.adb.getprop("ro.boot.flash.locked")
        if locked.strip() == "1" and not force:
            raise DeviceError("bootloader 是锁定的，fastboot flash 会失败",
                              hint="ro.boot.flash.locked=1；先解锁或加 --force 试")

        backup_path = None
        if backup:
            backup_path = self.backup_boot(partition)

        self.log.info("重启到 bootloader")
        self.adb.run("reboot", "bootloader", timeout=self.adb.cfg.timeout_short)
        serial = self.fb.pick()
        if not serial:
            raise DeviceError("设备没有进入 bootloader")

        self.log.info("刷 %s" % partition)
        r = self.fb.flash(partition, str(img.path), timeout=timeout)
        if not r.ok:
            # 失败时立刻尝试回滚，别把设备留在半刷状态
            if backup_path:
                self.log.warn("刷入失败，尝试回滚")
                rb = self.fb.flash(partition, str(backup_path))
                if rb.ok:
                    self.fb.reboot()
                    return Outcome(False, "flash_failed_rolled_back",
                                   "刷入失败，已回滚到备份",
                                   reasons=[r.text.strip()[:300]],
                                   data={"backup": str(backup_path)})
            return Outcome(False, "flash_failed", "刷入失败",
                           reasons=[r.text.strip()[:300],
                                    "ONYX 的 abl 裁了 flash handler 时，这条必然失败；"
                                    "改用 --via fastbootd 或 --via dd"],
                           data={"backup": str(backup_path) if backup_path else ""})

        self.fb.reboot()
        self.log.info("等待设备回到 adb")
        back = self.fb.wait_for_adb(seconds=wait_seconds)
        if not back:
            return Outcome(False, "reboot_timeout",
                           "刷入成功但 %d 秒内没回到 adb，可能卡在开机动画"
                           % wait_seconds,
                           data={"backup": str(backup_path) if backup_path else "",
                                 "partition": partition})
        return Outcome(True, "flashed", "刷入完成，设备已重启",
                       data={"partition": partition, "via": "fastboot",
                             "backup": str(backup_path) if backup_path else "",
                             "image": str(img.path)})

    # ---------------- 内置 Magisk ----------------
    def magisk_apk(self) -> Path | None:
        """内置 Magisk APK 的位置。

        实现放在 ops/magisk.py：那边是真正消费它的地方。
        这里延迟导入是躲 root <-> magisk 的循环（magisk 要用 parse_boot）。
        """
        from .magisk import find_apk
        return find_apk(self.adb.cfg)

    def install_magisk(self, reinstall: bool = True) -> Outcome:
        """只装内置的 Magisk App，并把「下一步」写成能照做的两行。

        默认只装 App：用户可能只是想拿 App 里的模块管理，不想动 boot 分区。
        要全自动（设备内打补丁 + dd 写回）走 `root magisk --full`，
        实现见 ops/magisk.py，那条路会真写 boot 分区。
        """
        apk = self.magisk_apk()
        if apk is None:
            return Outcome(False, "no_asset",
                           "这个构建里没有内置 Magisk APK",
                           reasons=["把 Magisk-*.apk 放进 EXE 同级的 assets\\magisk\\，"
                                    "或用 python build.py 重新打包"])

        # 装 APK 也是「动设备」：会覆盖用户已装的同名包，dry-run 下不能做。
        if self.adb.cfg.dry_run:
            raise Refused("dry-run：内置 Magisk 已找到（%s），未安装" % apk.name)

        pkg = "com.topjohnwu.magisk"
        already = self.adb.installed(pkg)
        self.log.info("内置 Magisk：%s（%.1f MB）"
                      % (apk.name, apk.stat().st_size / 1048576.0))
        r = self.adb.install(str(apk), allow_downgrade=None, reinstall=reinstall)
        if not r.ok:
            return Outcome(False, "install_failed",
                           "Magisk 安装失败：%s" % (r.text[:200] or "无输出"),
                           reasons=["设备上已装更高版本时会被拒，加 --no-reinstall 也会被拒",
                                    "先 adb uninstall com.topjohnwu.magisk 再试"],
                           data={"apk": str(apk), "was_installed": already})

        ver = self.adb.version_name(pkg)
        detail = ("Magisk %s 已装" % ver) if ver else "Magisk 已装"
        if already:
            detail = "Magisk 已重新安装（原版本被覆盖）"
        return Outcome(True, "installed", detail.strip(),
                       reasons=[
                           "下一步（在平板上操作）：打开 Magisk → 安装 → "
                           "选择并修补一个文件 → 选当前固件的 boot.img",
                           "修补产物在 /sdcard/Download/magisk_patched-*.img，"
                           "用 `l5tk root flash <文件>` 刷入当前槽位",
                       ],
                       data={"apk": str(apk), "was_installed": already})
