"""Root：状态判定 + 用 fastboot 刷已补丁的 boot 镜像。

刻意不做的事：V2 不自己打 Magisk 补丁，也不自己走 9008 写 flash。
  * Magisk 补丁在设备上用官方 App 打，产物 `magisk_patched-*.img`，可控可查。
  * 9008 需要与 SoC 匹配的 firehose programmer，Leaf5 是 LITO(SM6350)，
    Tab 系列是 BENGAL(SM6115)，V1 里那两份 lito programmer 在 Tab 上直接不认。
    把这条写进引导文案，比给一个会失败的分支有用。

所以 V2 的 root 路径是：adb root（userdebug）→ 不然 Magisk 补 boot → fastboot 刷。
每一步都留证据，刷之前必须能备份，备份不了就不刷。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from ..core.adb import Adb
from ..core.errors import DeviceError, ImageError, Refused
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
        su, su_detail, magisk, adb_root = detect_root(self.adb)
        locked = self.adb.getprop("ro.boot.flash.locked")
        state = "rooted" if (su or magisk) else ("adb_root" if adb_root else "none")
        return Outcome(
            ok=(state != "none"), stage=state,
            detail={"rooted": "已有 root（%s）" % (magisk or "su"),
                    "adb_root": "userdebug 可直接 adb root",
                    "none": "当前没有 root 权限%s"
                            % ("：%s" % su_detail if su_detail else "")}[state],
            data={"su": su, "magisk": magisk, "adb_root": adb_root,
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
    def flash_boot(self, image: str | Path, profile: Profile,
                   part: str | None = None, backup: bool = True,
                   force: bool = False, timeout: int | None = None,
                   wait_seconds: int = 180) -> Outcome:
        img = parse_boot(image)
        self.log.info("镜像校验通过：" + img.describe())

        # dry-run 必须挡在所有真动作之前：备份会 dd 块设备并 pull，
        # 刷入前还要 reboot 到 bootloader —— 这些都不是「只检查不动手」。
        if self.adb.cfg.dry_run:
            raise Refused("dry-run：镜像已校验，备份/重启/刷入均未执行")

        if not profile.verified and not force:
            raise DeviceError(
                "机型 %s 的档案没在真机验证过，拒绝刷入" % profile.label,
                hint="确认要刷就加 --force；刷错 boot 分区会导致设备无法开机",
            )

        locked = self.adb.getprop("ro.boot.flash.locked")
        partition, why = self.resolve_partition(profile, part)
        self.log.info("目标分区：%s（%s）" % (partition, why))
        if locked.strip() == "1" and not force:
            raise DeviceError("bootloader 是锁定的，fastboot flash 会失败",
                              hint="ro.boot.flash.locked=1；先解锁或加 --force 试")

        backup_path = None
        if backup:
            backup_path = self.backup_boot(partition)

        steps = []
        self.log.info("重启到 bootloader")
        self.adb.run("reboot", "bootloader", timeout=self.adb.cfg.timeout_short)
        serial = self.fb.pick()
        if not serial:
            raise DeviceError("设备没有进入 bootloader")

        self.log.info("刷 %s" % partition)
        r = self.fb.flash(partition, str(image), timeout=timeout)
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
                           reasons=[r.text.strip()[:300]],
                           data={"backup": str(backup_path) if backup_path else ""})

        self.fb.reboot()
        self.log.info("等待设备回到 adb")
        ok = self.fb.wait_for_adb(seconds=wait_seconds)
        if not ok:
            return Outcome(False, "reboot_timeout",
                           "刷入成功但 180 秒内没回到 adb，可能卡在开机动画",
                           data={"backup": str(backup_path) if backup_path else "",
                                 "partition": partition})
        return Outcome(True, "flashed", "刷入完成，设备已重启",
                       data={"partition": partition,
                             "backup": str(backup_path) if backup_path else "",
                             "image": str(img.path)})

    # ---------------- 候选镜像 ----------------
    def candidates(self, folder: str | Path | None = None) -> list[Path]:
        return list_boot_images(folder or (self.adb.cfg.base_dir / "boot"))
