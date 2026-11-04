"""整机固件备份 / 恢复。

`root magisk` 只备份 boot 一个分区，够它自己用，不够自救：真刷坏不一定在 boot ——
vbmeta 写错卡开机、super 写一半没有系统、dtbo 不对黑屏。手边要有一份
「逐分区能写回去」的镜像集，并且自带元数据（哪个块设备、多少字节、md5 是多少）。

三条硬规矩：
  1. 备份完必须能自证：设备侧 dd 出文件算一次 md5，拉回本地后流式解压再算一次，
     两边对上才算备份成立。断在 33% 的残片不会被当成备份。
  2. 恢复前必须再备份目标分区（`--no-safety` 可关）：写完发现镜像选错了，
     还能拿刚才那份退回去。
  3. 写回后必须回读分区 md5 并比对：dd 返回 0 只说明命令没报错，不说明写进去了。

为什么 userdata 不在默认集里：几十 GB，而且它不是「刷坏」的对象 ——
刷机不会动它，恢复它反而是最容易误伤的步骤。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from ..core.adb import Adb
from ..core.errors import DeviceError

SCHEMA = 1
CHUNK = 2 << 20
DEV_DIR = "/data/local/tmp/_l5fw"

# 启动链：写坏任意一个都开不了机
BOOT_GROUP = (
    "boot", "init_boot", "vendor_boot", "recovery",
    "vbmeta", "vbmeta_system", "vbmeta_vendor", "dtbo",
)

# 系统链：启动链 + 能把 Android 装回去的那几个
CRITICAL_GROUP = BOOT_GROUP + (
    "super", "system", "system_ext", "vendor", "product", "odm",
    "misc", "persist", "modem", "radio", "lk", "tee",
)

PRESETS = {"boot": BOOT_GROUP, "critical": CRITICAL_GROUP, "all": None}

# 列出分区 + 真实块设备 + 字节数。
# blockdev 不一定有（部分 toybox 裁了），退回 /sys/class/block/<dev>/size（512 字节扇区）。
_LIST_SCRIPT = r"""
for p in /dev/block/by-name/*; do
  n=${p##*/}
  r=$(readlink -f "$p" 2>/dev/null)
  s=$(blockdev --getsize64 "$p" 2>/dev/null)
  if [ -z "$s" ] || [ "$s" = "0" ]; then
    b=${r##*/}
    s=$(( $(cat "/sys/class/block/$b/size" 2>/dev/null || echo 0) * 512 ))
  fi
  echo "$n|$s|$r"
done
"""


@dataclass
class Part:
    name: str
    dev: str
    size: int

    @property
    def mb(self) -> float:
        return self.size / 1048576.0


def _md5_file(p: str | Path, limit: int | None = None) -> str:
    h = hashlib.md5()
    n = 0
    with open(str(p), "rb") as f:
        while True:
            buf = f.read(1 << 20)
            if not buf:
                break
            h.update(buf)
            n += len(buf)
            if limit is not None and n >= limit:
                break
    return h.hexdigest()


def _md5_gz(p: str | Path) -> tuple[str, int]:
    """流式解压算 md5 和字节数，不落盘。"""
    h = hashlib.md5()
    n = 0
    with gzip.open(str(p), "rb") as f:
        while True:
            buf = f.read(1 << 20)
            if not buf:
                break
            h.update(buf)
            n += len(buf)
    return h.hexdigest(), n


def require_root(adb: Adb, what: str) -> None:
    ok, why = adb.su_probe()
    if ok:
        return
    raise DeviceError(
        "没法%s：当前没有 root" % what,
        hint=(why or "su 不可用") + "；先 `l5tk root auto`（临时 root）"
             "或 `l5tk root magisk --full`（持久）",
    )


def list_partitions(adb: Adb) -> list[Part]:
    """读设备分区表。必须已 root —— 未 root 时 by-name 目录读不全。"""
    r = adb.sh(_LIST_SCRIPT, timeout=adb.cfg.timeout_normal, root=True)
    if not r.ok:
        raise DeviceError("读分区表失败", hint=(r.err or r.out).strip()[:200])
    out: list[Part] = []
    for line in r.out.splitlines():
        bits = line.strip().split("|")
        if len(bits) < 3 or not bits[0]:
            continue
        try:
            size = int(bits[1])
        except ValueError:
            continue
        if size <= 0:
            continue
        out.append(Part(bits[0], bits[2], size))
    if not out:
        raise DeviceError(
            "分区表是空的",
            hint="`ls -l /dev/block/by-name/` 手工看一眼；A/B 设备名字带 _a/_b",
        )
    return sorted(out, key=lambda p: p.name)


def pick_parts(all_parts: list[Part], spec: str | None) -> list[Part]:
    """把 `--part` 的取值解析成分区列表。

    取值三种：组名（boot / critical / all）、单个分区名（boot_b / super）、
    逗号分隔的多个名字。组名里的每一项都按「base、base_a、base_b」展开，
    这样同一份预设能同时用在单槽和 A/B 设备上。
    """
    spec = (spec or "critical").strip()
    names = {p.name for p in all_parts}
    by_name = {p.name: p for p in all_parts}

    if spec in PRESETS:
        group = PRESETS[spec]
        if group is None:
            return list(all_parts)
        picked: list[Part] = []
        for base in group:
            for cand in (base, base + "_a", base + "_b"):
                if cand in names:
                    picked.append(by_name[cand])
        # 同一分区只出现一次（boot_a 与 boot_a 不会重复，但保险）
        seen, uniq = set(), []
        for p in picked:
            if p.name not in seen:
                seen.add(p.name)
                uniq.append(p)
        return uniq

    want: list[str] = []
    for token in spec.replace(" ", "").split(","):
        if not token:
            continue
        if token in names:
            want.append(token)
            continue
        expanded = [c for c in (token + "_a", token + "_b") if c in names]
        if expanded:
            want.extend(expanded)
            continue
        raise DeviceError(
            "设备上没有分区 %s" % token,
            hint="可用：%s" % ", ".join(sorted(names)),
        )
    return [by_name[n] for n in dict.fromkeys(want)]


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------

def _dump_one(adb: Adb, log, part: Part, dest: Path, gz: bool = True) -> dict:
    """把一个分区 dd 出来、拉回 PC、双向校验。返回 manifest 里的一条。"""
    remote = "%s/%s.img" % (DEV_DIR, part.name)
    t0 = time.time()
    log.info("  %-16s %6.1f MB → 设备内 dd…" % (part.name, part.mb))

    adb.sh("mkdir", "-p", DEV_DIR, timeout=adb.cfg.timeout_short, root=True)
    adb.sh("chmod", "777", DEV_DIR, timeout=adb.cfg.timeout_short, root=True)

    # 读到分区末尾才算完；bs=4M 是 toybox dd 认的最大常规块
    cmd = ('dd if=/dev/block/by-name/%s of=%s bs=4M 2>&1; echo RC=$?'
           % (part.name, remote))
    timeout = max(300, int(part.size / (4 << 20)) + 180)
    r = adb.sh(cmd, timeout=timeout, root=True)
    if "RC=0" not in (r.out or ""):
        raise DeviceError(
            "dd 备份 %s 失败" % part.name,
            hint=((r.out or r.err or "无输出").strip().splitlines() or [""])[-1][:200],
        )

    got = adb.dev_size(remote)
    if got != part.size:
        raise DeviceError(
            "dd 产物 %d 字节，分区是 %d 字节" % (got, part.size),
            hint="分区可能被并发写入（系统在跑）；重启后再备份",
        )

    dev_md5 = (adb.sh_out("md5sum " + remote, timeout=max(120, int(part.mb) + 60),
                          root=True).split() or [""])[0]
    if not dev_md5:
        raise DeviceError("读不到设备侧 md5（%s）" % part.name)

    # 传输：gz 压一遍再走分块 pull。96 MB 分区直接拉会断在 30% 左右，
    # 而且 adb 返回 rc=0 —— 这条链路上压缩换带宽是唯一稳的做法。
    if gz:
        gzr = remote + ".gz"
        c = 'gzip -1 -c "%s" > "%s" && chmod 644 "%s"' % (remote, gzr, gzr)
        rc = adb.sh(c, timeout=max(300, int(part.mb) + 120), root=True)
        if not rc.ok:
            raise DeviceError("设备侧压缩失败",
                              hint=(rc.err or rc.out).strip()[:200])
        gzs = adb.dev_size(gzr)
        if gzs < 1024:
            raise DeviceError("压缩包只有 %d 字节，gzip 没读到输入" % gzs)

        local_gz = dest / ("%s.img.gz" % part.name)
        adb.pull(gzr, str(local_gz), expect_size=gzs)
        local_md5, local_size = _md5_gz(local_gz)
        fname, fsize = local_gz.name, local_gz.stat().st_size
        adb.sh("rm", "-f", gzr, remote, timeout=adb.cfg.timeout_short, root=True)
    else:
        local_img = dest / ("%s.img" % part.name)
        adb.pull(remote, str(local_img), expect_size=part.size)
        local_md5 = _md5_file(local_img)
        local_size = local_img.stat().st_size
        fname, fsize = local_img.name, local_size
        adb.sh("rm", "-f", remote, timeout=adb.cfg.timeout_short, root=True)

    if local_size != part.size:
        raise DeviceError("%s 拉回后 %d 字节，分区是 %d" % (part.name, local_size, part.size))
    if local_md5 != dev_md5:
        raise DeviceError(
            "%s 校验失败：设备 %s != 本地 %s" % (part.name, dev_md5[:12], local_md5[:12]),
            hint="USB 链路在传输中损坏了数据，这一份不能用；重跑一次备份",
        )

    log.ok("  %-16s %6.1f MB  md5 %s  %.0fs"
           % (part.name, part.mb, local_md5[:12], time.time() - t0))
    return {
        "name": part.name, "dev": part.dev, "size": part.size,
        "md5": local_md5, "file": fname, "file_size": fsize, "gz": bool(gz),
    }


def _device_info(adb: Adb) -> dict:
    keys = ("ro.product.model", "ro.product.device", "ro.build.display.id",
            "ro.build.version.release", "ro.boot.slot_suffix", "ro.serialno")
    try:
        props = adb.props(keys)
    except Exception:                                    # 读不到不算失败
        props = {}
    try:
        ver = adb.sh_out("magisk", "-v", timeout=adb.cfg.timeout_short, root=True)
    except Exception:
        ver = ""
    return {
        "model": props.get("ro.product.model", ""),
        "device": props.get("ro.product.device", ""),
        "build": props.get("ro.build.display.id", ""),
        "android": props.get("ro.build.version.release", ""),
        "slot": props.get("ro.boot.slot_suffix", "").lstrip("_"),
        "serial": props.get("ro.serialno", ""),
        "magisk": ver,
    }


def backup(adb: Adb, log, out_dir: str | Path, part_spec: str = "critical",
           gz: bool = True, dry_run: bool = False) -> Path:
    """备份指定分区到 out_dir 下的一个新目录，返回该目录。"""
    require_root(adb, "备份固件")
    all_parts = list_partitions(adb)
    picked = pick_parts(all_parts, part_spec)
    if not picked:
        raise DeviceError(
            "按 %s 没有匹配到任何分区" % part_spec,
            hint="设备上有：%s" % ", ".join(p.name for p in all_parts),
        )

    total = sum(p.size for p in picked)
    log.info("将备份 %d 个分区，合计 %.1f MB" % (len(picked), total / 1048576))
    for p in picked:
        log.info("  %-18s %8.1f MB  %s" % (p.name, p.mb, p.dev))
    if dry_run:
        return Path("")

    info = _device_info(adb)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    tag = (info.get("device") or "device").replace(" ", "_")
    dest = Path(out_dir) / ("l5fw_%s_%s" % (tag, stamp))
    dest.mkdir(parents=True, exist_ok=True)

    entries = []
    for i, p in enumerate(picked, 1):
        log.step(i, len(picked), p.name)
        entries.append(_dump_one(adb, log, p, dest, gz=gz))

    manifest = {
        "schema": SCHEMA,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "device": info,
        "preset": part_spec,
        "parts": entries,
        "total_bytes": sum(e["size"] for e in entries),
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    log.ok("备份完成：%s（%d 个分区，%.1f MB）"
           % (dest, len(entries), manifest["total_bytes"] / 1048576))
    return dest


# ---------------------------------------------------------------------------
# 恢复
# ---------------------------------------------------------------------------

def read_manifest(src: str | Path) -> dict:
    p = Path(src)
    if p.is_file():
        p = p.parent
    mf = p / "manifest.json"
    if not mf.is_file():
        raise DeviceError(
            "%s 里没有 manifest.json" % p,
            hint="恢复只认 l5tk backup 产出的目录（缺元数据就不敢往分区里写）",
        )
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise DeviceError("manifest.json 读不出来：%s" % e)
    if data.get("schema") != SCHEMA:
        raise DeviceError(
            "manifest 版本 %s，本工具只认 %d" % (data.get("schema"), SCHEMA),
            hint="备份是新版工具做的就升级，是旧版就重新备份",
        )
    if not data.get("parts"):
        raise DeviceError("manifest 里没有任何分区")
    return data


def _restore_one(adb: Adb, log, entry: dict, src: Path, cur: Part,
                 safety: bool, force: bool) -> str:
    name = entry["name"]
    f = src / entry["file"]
    if not f.is_file():
        raise DeviceError("缺文件：%s" % f)

    if entry["gz"]:
        img_md5, img_size = _md5_gz(f)
    else:
        img_size = f.stat().st_size
        img_md5 = _md5_file(f)
    if img_md5 != entry["md5"]:
        raise DeviceError(
            "%s 的镜像损坏：md5 %s != manifest %s"
            % (name, img_md5[:12], entry["md5"][:12]),
            hint="备份目录里的文件被改动或传输中损坏了，换一份备份",
        )
    if img_size != entry["size"]:
        raise DeviceError("%s 镜像 %d 字节，manifest 记的是 %d"
                          % (name, img_size, entry["size"]))

    # 写不下就永远拒绝：分区比镜像小，dd 会静默截断，等于写废
    if img_size > cur.size:
        raise DeviceError(
            "%s 镜像 %.1f MB，设备分区只有 %.1f MB" % (name, img_size / 1048576.0,
                                                      cur.mb),
            hint="这份备份不是这台机器的，或者分区表已经变了",
        )
    if img_size < cur.size and not force:
        log.warn("  %s 镜像比分区小 %.1f MB（分区尾部会保留旧数据）"
                 % (name, (cur.size - img_size) / 1048576.0))

    if safety:
        log.info("  %s 先备份当前内容…" % name)
        sdir = src / ("_safety_" + time.strftime("%Y%m%d_%H%M%S"))
        sdir.mkdir(parents=True, exist_ok=True)
        prev = _dump_one(adb, log, cur, sdir, gz=True)
        log.info("  回滚用：%s" % (sdir / prev["file"]))

    # 解压到临时 img 再推（push 只认普通文件）
    tmpdir = Path(adb.cfg.work_dir) / "_restore_tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    local_img = tmpdir / ("%s.img" % name)
    if entry["gz"]:
        with gzip.open(str(f), "rb") as s, open(str(local_img), "wb") as d:
            shutil.copyfileobj(s, d, 1 << 20)
    else:
        shutil.copyfile(str(f), str(local_img))

    try:
        if local_img.stat().st_size != img_size:
            raise DeviceError("%s 解压后 %d 字节，应为 %d"
                              % (name, local_img.stat().st_size, img_size))
        log.info("  %s 推送 %.1f MB…" % (name, img_size / 1048576.0))
        adb.sh("mkdir", "-p", DEV_DIR, timeout=adb.cfg.timeout_short, root=True)
        adb.sh("chmod", "777", DEV_DIR, timeout=adb.cfg.timeout_short, root=True)
        remote = "%s/%s.img" % (DEV_DIR, name)
        adb.push(str(local_img), remote, expect_size=img_size)
        adb.sh("chmod", "644", remote, timeout=adb.cfg.timeout_short, root=True)

        log.info("  %s 写回分区 %s…" % (name, cur.dev))
        cmd = ('dd if=%s of=/dev/block/by-name/%s bs=4M conv=fsync 2>&1; echo RC=$?'
               % (remote, name))
        r = adb.sh(cmd, timeout=max(300, int(img_size / (4 << 20)) + 180), root=True)
        if "RC=0" not in (r.out or ""):
            raise DeviceError(
                "写入 %s 失败" % name,
                hint=((r.out or r.err or "无输出").strip().splitlines() or [""])[-1][:200],
            )

        # 回读：dd 返回 0 只说明命令没报错
        back = (adb.sh_out("md5sum /dev/block/by-name/%s" % name,
                           timeout=max(120, int(img_size / 1048576.0) + 60),
                           root=True).split() or [""])[0]
        if back != entry["md5"]:
            raise DeviceError(
                "%s 回读 md5 %s != %s" % (name, back[:12], entry["md5"][:12]),
                hint="这一份已经写进去了但内容不对；用刚才的 _safety 备份恢复",
            )
        adb.sh("rm", "-f", remote, timeout=adb.cfg.timeout_short, root=True)
        return "ok"
    finally:
        try:
            os.remove(str(local_img))
        except OSError:
            pass


def restore(adb: Adb, log, src: str | Path, only: str | None = None,
            safety: bool = True, force: bool = False,
            dry_run: bool = False) -> list[tuple[str, str, str]]:
    """从备份目录写回设备。返回 [(分区, 结果, 说明)]。"""
    src_dir = Path(src)
    if src_dir.is_file():
        src_dir = src_dir.parent
    mf = read_manifest(src_dir)

    require_root(adb, "恢复固件")
    all_parts = list_partitions(adb)
    by_name = {p.name: p for p in all_parts}

    entries = mf["parts"]
    if only:
        wanted = {p.name for p in pick_parts(all_parts, only)}
        entries = [e for e in entries if e["name"] in wanted]
        if not entries:
            raise DeviceError("备份里没有 %s 这些分区" % only)

    missing = [e["name"] for e in entries if e["name"] not in by_name]
    if missing:
        raise DeviceError(
            "设备上没有这些分区：%s" % ", ".join(missing),
            hint="这份备份来自另一台机器？分区名对不上就不写",
        )

    log.info("将恢复 %d 个分区 → %s" % (len(entries), src_dir))
    for e in entries:
        log.info("  %-18s %8.1f MB  md5 %s"
                 % (e["name"], e["size"] / 1048576.0, e["md5"][:12]))
    if dry_run:
        return []

    results: list[tuple[str, str, str]] = []
    for i, e in enumerate(entries, 1):
        log.step(i, len(entries), e["name"])
        try:
            _restore_one(adb, log, e, src_dir, by_name[e["name"]], safety, force)
            results.append((e["name"], "ok", ""))
            log.ok("  %s 写入并回读校验通过" % e["name"])
        except DeviceError as ex:
            results.append((e["name"], "fail", ex.message))
            log.err("  %s 失败：%s" % (e["name"], ex.message))
            break        # 一个写坏就停：继续写别的只会把状态搅乱

    ok_n = sum(1 for _, s, _ in results if s == "ok")
    if ok_n == len(entries):
        log.ok("全部 %d 个分区已恢复。重启后生效。" % ok_n)
    else:
        log.warn("恢复中断：%d/%d 成功，其余未动" % (ok_n, len(entries)))
    return results
