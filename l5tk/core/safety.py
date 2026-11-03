"""刷机前的硬性安全闸：没有可用备份，不许往分区写一个字节。

为什么做成「不可关」而不是「默认开、可关」：

  `--no-backup` 跳过的那一次，敲命令的人一定记得自己跳过了。但下次顺手
  复用同一条命令行、或者把它写进脚本之后，那个开关就悄悄成了默认行为 ——
  出事时既没有备份，屏幕上也没有任何提示。所以这里不提供关闭能力。

三层都拦，防止将来从别的入口绕过去：

  1. 参数层   cli.py 收到 --no-backup 直接拒，退出码非 0
  2. 调用层   flash() / flash_boot() 收到 backup=False 直接拒
  3. 备份本身 备份读不满、md5 对不上、文件没落地 → 中止，不是 warn 之后继续

第 3 条是最容易被写成「警告后继续」的地方：备份失败时刷入往往也能成功，
日志里只有一行 warn,等真出事回头找备份才发现是个空文件。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .errors import Refused

# 备份文件至少这么大才可能是真的 boot 分区。boot_b 在 Leaf5 上是 96MB，
# 别的机型也不会小到 1MB 以下；比这还小基本是读失败留下的残file。
MIN_BACKUP_BYTES = 1 << 20


class BackupRequired(Refused):
    """没有可用备份就要写分区。"""


def require_backup(backup: bool, where: str = "写分区") -> None:
    """backup=False 直接拒。调用方不再有「跳过备份」这条路。"""
    if backup:
        return
    raise BackupRequired(
        "%s之前必须备份原分区，已取消（未写入任何字节）" % where,
        hint="刷坏的 boot 是唯一无法自救的分区 —— 没有原厂镜像就只能整机重刷。"
             "备份要几十秒到几分钟，比救砖短得多",
        code="backup_required",
    )


def check_backup_file(path: str | Path, expect_bytes: int | None = None) -> Path:
    """备份文件落盘后的验收：必须存在、不能小得不像分区。

    任何一条不成立都抛 BackupRequired —— 这一层的意义是：宁可不刷，
    也不要拿着一个 0 字节的「备份」给自己壮胆。
    """
    p = Path(path)
    if not p.is_file():
        raise BackupRequired(
            "备份文件没生成：%s，已中止（分区未改动）" % p,
            hint="USB 链路不稳或分区读权限不足；换线/换口后重试",
            code="backup_missing",
        )
    got = p.stat().st_size
    if got < MIN_BACKUP_BYTES:
        raise BackupRequired(
            "备份只有 %d 字节，不像完整分区，已中止（分区未改动）" % got,
            hint="备份读失败时不会报错，只会留下一个短文件；别拿它当退路",
            code="backup_too_small",
        )
    if expect_bytes is not None and got != expect_bytes:
        raise BackupRequired(
            "备份长度不对：%d != %d，已中止（分区未改动）" % (got, expect_bytes),
            hint="USB 链路在掉包时不写 —— 没有可信备份就没有退路",
            code="backup_short",
        )
    return p


def sha256_file(path: str | Path, limit: int | None = None) -> str:
    """算 sha256。limit 只读前 N 字节，用于「只校验镜像长度那一段」。"""
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
