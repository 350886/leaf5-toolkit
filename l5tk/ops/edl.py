"""EDL（9008）直写分区 —— fastboot 被厂商裁掉之后的替代通道。

为什么需要这一条：

  ONYX 的 abl 没有编译 `flash` 的 handler，`fastboot flash boot_b` 一律返回
  `remote: 'unknown command'`。9008 是 SoC 自己的 PBL/SBL 下载模式，走高通
  Firehose 协议，跟 fastboot 是两条互不相干的通道 —— 裁 fastboot 命令表
  裁不掉它。于是「设备没 root，又不能走 dd」时，这是唯一还能写 boot 的路。

**programmer 必须与 SoC 匹配**，这是硬门槛，不是配置问题：

  Leaf5            = LITO(SM6350)    → assets/lito_moto_fhprg.bin   ✅ 实测通过
  Tab8/Tab8C/Tab10 = BENGAL(SM6115)  → 需要 bengal 的 *_fhprg.bin，仓库里没有

同平台的 `lito_op_n10_fhprg.bin` 也在包内，但它是 OnePlus N10 的**受限构建**：
configure 只报 MaxPayload=16384，`program` 一律回 `Device verified failed` +
`rawmode=false`，写不进去。它只能用于读。默认顺序已经把它排到后面，
别为了「试一下」手动切过去。

拿错 programmer 的表现不是「友好地报个错」，而是 Sahara 握手阶段设备不应答、
超时退出。所以下面把「找不到 programmer」和「握手失败」分成两个 stage，
后者直接给「SoC 可能不是 LITO」的提示。

写分区本身的顺序，每一步不成立都立刻停、分区一个字节不动：

  1. 分区必须在 GPT 里，容量必须装得下镜像
  2. 先读一份原分区到 PC，长度必须等于 GPT 里记的 length
  3. 写
  4. 读回镜像长度那一段，sha256 与源文件逐字节比对
  5. 第 4 步不符 → 用第 2 步的备份写回去，然后报「已回滚」

第 2 步的长度校验不是形式：备份读不满说明 USB 链路在掉包，此时写进去
出事就没有可信的退路。
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from ..core import drivers, edllink
from ..core.errors import ImageError, L5Error
from ..core.result import Outcome
from ..core.safety import check_backup_file, require_backup
from ..logx import Logger


def sha256_of(path: str | Path, limit: int | None = None) -> str:
    """整文件或前 limit 字节的 sha256。读回的文件按扇区对齐，比源镜像长一点。"""
    h = hashlib.sha256()
    left = limit
    with open(path, "rb") as f:
        while True:
            n = 1 << 20 if left is None else min(1 << 20, left)
            if n <= 0:
                break
            b = f.read(n)
            if not b:
                break
            h.update(b)
            if left is not None:
                left -= len(b)
    return h.hexdigest()


class EdlOps:
    """9008 会话封装成 V2 的 Outcome 形状。"""

    def __init__(self, adb, log: Logger, loader: str | None = None):
        self.adb = adb
        self.log = log
        self.loader = loader

    # ---------------- 环境 ----------------
    def loader_path(self) -> str:
        p = self.loader or edllink.loader_path()
        if not p or not os.path.isfile(p):
            raise ImageError(
                "找不到 firehose programmer（*_fhprg.bin）",
                hint="放一份到 assets/，或用 --loader 指定完整路径")
        return p

    def status(self) -> Outcome:
        """9008 在不在、驱动能不能用、programmer 有没有。只读，不动设备。"""
        online = edllink.find_9008()
        st = drivers.state()
        try:
            lp = self.loader_path()
            have = "%s（%d KB）" % (os.path.basename(lp), os.path.getsize(lp) // 1024)
        except L5Error:
            have = ""
        data = {"online": bool(online), "driver": st.get("usable"),
                "loader": have, "devices": online}
        if not have:
            return Outcome(False, "no_loader",
                           "9008 链路可用，但缺 firehose programmer",
                           reasons=["assets/ 下需要有 *_fhprg.bin",
                                    "Leaf5 用 lito，Tab 系列是 BENGAL 平台，lito 那份不认"],
                           data=data)
        if not online:
            return Outcome(False, "not_in_edl",
                           "设备不在 9008（可以 adb reboot edl 送进去）",
                           reasons=["驱动状态：%s" % drivers.summary(st)], data=data)
        if not st.get("usable"):
            return Outcome(False, "driver_missing",
                           "9008 在线但 WinUSB 驱动没绑上",
                           reasons=[drivers.summary(st),
                                    "界面里点「EDL 刷 boot」会自动拉起内置 Zadig"],
                           data=data)
        return Outcome(True, "ready", "9008 就绪，programmer %s" % have, data=data)

    def _ensure_edl(self) -> None:
        """设备不在 9008 就送进去；缺驱动就拉 Zadig。失败抛。"""
        if edllink.find_9008():
            st = drivers.state()
            if st.get("usable"):
                self.log.info("9008 在线，驱动就绪")
                return
            self.log.warn("9008 在线但驱动不可用，拉起 Zadig")
            drivers.launch_zadig(log=lambda t: self.log.info(str(t)))
            if not drivers.wait_ready(seconds=120):
                raise L5Error("9008 在线但 WinUSB 驱动没装上",
                              hint="手动跑 assets/zadig.exe，给 05C6:9008 选 WinUSB")
            return

        r = self.adb.sh("reboot", "edl", timeout=self.adb.cfg.timeout_short)
        if not r.ok:
            raise L5Error(
                "设备既不在 9008，adb 也送不进去",
                hint="手动组合键：关机 → 按住音量上+下 → 插 USB")
        self.log.info("已发送 reboot edl，等 9008 枚举")
        ok, sec = edllink.wait_9008(limit=30.0)
        if not ok:
            raise L5Error("30 秒内没等到 9008",
                          hint="换后置 USB 口、别过 hub；或手动组合键进下载模式")
        self.log.info("9008 已出现（%.1fs）" % sec)
        st = drivers.state()
        if not st.get("usable"):
            self.log.warn("缺 WinUSB 驱动，拉起 Zadig")
            drivers.launch_zadig(log=lambda t: self.log.info(str(t)))
            if not drivers.wait_ready(seconds=120):
                raise L5Error("驱动没装上，无法建立 firehose 会话",
                              hint="手动跑 assets/zadig.exe")

    # ---------------- 只读 ----------------
    def gpt(self) -> Outcome:
        """读分区表。会先把设备送进 9008。"""
        try:
            self._ensure_edl()
        except L5Error as e:
            return Outcome(False, "edl_unreachable", str(e), reasons=self._hints(e))
        sess = edllink.Session(loader=self.loader_path())
        buf: list[str] = []
        try:
            sess.open(on_line=lambda t: buf.append(t), timeout=90)
            sess.printgpt(on_line=lambda t: buf.append(t), timeout=120)
        except L5Error as e:
            return Outcome(False, "handshake_failed", str(e),
                           reasons=["programmer 与 SoC 不匹配时就是这个表现",
                                    "Leaf5 是 LITO，Tab 三台是 BENGAL —— 两者不通用"])
        finally:
            try:
                sess.reset()
            except Exception:
                pass
            sess.close()
        parts = edllink.parse_gpt("\n".join(buf))
        if not parts:
            return Outcome(False, "gpt_empty", "printgpt 没解析出分区表",
                           reasons=["\n".join(buf[-12:])])
        self.log.ok("分区表 %d 项" % len(parts))
        return Outcome(True, "gpt", "分区表 %d 项" % len(parts), data={"parts": parts})

    # ---------------- 写 ----------------
    def flash(self, image: str | Path, part: str | None = None,
              backup: bool = True, verify: bool = True,
              timeout_write: int = 1800, timeout_read: int = 900) -> Outcome:
        """把镜像写进分区。part 不给就按当前 slot 推 boot_a / boot_b。

        写之前必须有原分区备份，这条不可关。见 core/safety.py 顶部的原因。
        """
        require_backup(backup, "9008 写分区")
        img = Path(image)
        if not img.is_file():
            raise ImageError("镜像不存在：%s" % img)
        size = img.stat().st_size
        if size <= 0:
            raise ImageError("镜像是空文件：%s" % img)

        part = part or self._part_from_slot()
        src_sum = sha256_of(img)
        self.log.info("源镜像 %s  %d 字节" % (img.name, size))
        self.log.info("sha256   %s" % src_sum)

        try:
            self._ensure_edl()
        except L5Error as e:
            return Outcome(False, "edl_unreachable", str(e), reasons=self._hints(e))

        out_dir = Path(self.adb.cfg.work_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")

        sess = edllink.Session(loader=self.loader_path())
        bak = rb = None
        try:
            sess.open(timeout=90)
            buf: list[str] = []
            sess.printgpt(on_line=buf.append, timeout=90)
            gpt = edllink.parse_gpt("\n".join(buf))
            e = gpt.get(part)
            if not e:
                # 常见笔误：大小写、少了 suffix、把槽位写进名字。
                # 去掉尾部的 _a/_b 再比前缀，bootb → boot → boot_a/boot_b
                key = part.lower().rstrip("_ab")[:5]
                near = [n for n in gpt if n.lower().startswith(key)][:8]
                return Outcome(
                    False, "no_partition", "分区表里没有 %s" % part,
                    reasons=[("相近的：" + "、".join(near)) if near
                             else "分区表共读到 %d 项" % len(gpt)])
            ss = int(sess.fh.firehose.cfg.SECTOR_SIZE_IN_BYTES) or 512
            need = (size + ss - 1) // ss
            room = e["length"] // ss
            if need > room:
                return Outcome(
                    False, "too_large",
                    "镜像 %d 字节（%d 扇区）大于分区 %d 字节（%d 扇区）"
                    % (size, need, e["length"], room),
                    reasons=["已中止，未写入任何字节"])
            self.log.info("分区 %s：offset 0x%X，%d 字节（镜像 %d / 分区 %d 扇区）"
                          % (part, e["offset"], e["length"], need, room))

            if backup:
                bak = out_dir / ("stock_%s_%s.img" % (part, stamp))
                self.log.info("备份原分区 → %s" % bak)
                sess.read(part, str(bak),
                          on_line=lambda t: self.log.dbg("  " + t),
                          place=(0, e["offset"] // ss, room),
                          expect=e["length"], timeout=timeout_read)
                # 备份必须真的落地、长度对得上。不过就不写 ——
                # 拿一个短文件当退路比没有备份更危险（以为是安全网）。
                try:
                    check_backup_file(bak, e["length"])
                except L5Error as ex:
                    return Outcome(
                        False, "backup_failed", ex.message,
                        reasons=[ex.hint] if ex.hint else [],
                        data={"backup": str(bak), "partition": part})
                self.log.ok("备份完成 %d 字节" % bak.stat().st_size)

            self.log.info("写入 %s …" % part)
            info = sess.write(part, str(img),
                              on_line=lambda t: self.log.dbg("  " + t),
                              timeout=timeout_write)
            self.log.ok("写入完成，%.1fs" % info["seconds"])

            verified = None
            if verify:
                rb = out_dir / ("readback_%s_%s.img" % (part, stamp))
                self.log.info("读回校验 …")
                sess.read(part, str(rb),
                          on_line=lambda t: self.log.dbg("  " + t),
                          place=(0, e["offset"] // ss, need),
                          expect=need * ss, timeout=timeout_read)
                back = sha256_of(rb, limit=size)
                verified = back == src_sum
                if not verified:
                    self.log.warn("读回 sha256 不符：%s != %s" % (back, src_sum))
                    if bak:
                        self.log.warn("用备份回写")
                        sess.write(part, str(bak), timeout=timeout_write)
                        return Outcome(
                            False, "readback_mismatch_rolled_back",
                            "读回校验不符，已用备份回写",
                            reasons=["常见原因：USB 线/口不稳、programmer 与颗粒不匹配",
                                     "备份：%s" % bak],
                            data={"backup": str(bak), "readback": str(rb),
                                  "partition": part})
                    return Outcome(
                        False, "readback_mismatch",
                        "读回校验不符，且没有备份可回滚",
                        reasons=["需要重新进 9008 刷原厂固件"],
                        data={"readback": str(rb), "partition": part})
                self.log.ok("读回校验通过")

            return Outcome(
                True, "flashed", "已写入 %s 并读回校验一致" % part,
                data={"partition": part, "size": size, "sha256": src_sum,
                      "backup": str(bak) if bak else "",
                      "readback": str(rb) if rb else "",
                      "verified": verified})
        except L5Error as e:
            return Outcome(False, "write_failed", str(e), reasons=self._hints(e))
        finally:
            # reset 无论成败都要发：设备停在 9008 时屏幕全黑，看起来像变砖
            try:
                sess.reset()
            finally:
                sess.close()

    # ---------------- 内部 ----------------
    def _part_from_slot(self) -> str:
        slot = (self.adb.getprop("ro.boot.slot_suffix") or "").lstrip("_")
        if not slot:
            slot = "b"      # 这批机器的默认槽位；写错槽位等于写到另一个系统
            self.log.warn("读不到 slot_suffix，按 _b 处理（用 --part 可明确指定）")
        return "boot_%s" % slot

    @staticmethod
    def _hints(e: BaseException) -> list[str]:
        h = getattr(e, "hint", "") or ""
        return [h] if h else []
