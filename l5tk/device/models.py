"""机型档案的数据形状。

为什么档案是数据而不是分支：V1 里 `if "Leaf5" in model:` 散落在七个函数里，
加一台 Tab 就要改七处。V2 把差异全收进 Profile 表，加机型 = 加一行数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Pattern


@dataclass(frozen=True)
class Profile:
    key: str                      # leaf5 / tab8c / tab8 / tab10 / generic
    label: str                    # 给人看的名字
    models: tuple[Pattern, ...] = ()     # ro.product.model 的正则
    aliases: tuple[str, ...] = ()        # ro.product.device / ro.product.name 精确值
    soc_keys: tuple[str, ...] = ()       # ro.board.platform 等小写值
    soc_label: str = ""
    family: str = ""                     # 同一家族共享机型名时用来收口
    android_major: int = 0               # 0 = 不校验
    build_type: str = ""                 # user / userdebug，空 = 不校验
    ab_slot: bool | None = None          # None = 不校验
    boot_part: str = "boot"              # 单槽默认 boot，A/B 用 boot_a
    verified: bool = False               # 是否在真机上实测过
    notes: tuple[str, ...] = field(default=())

    def desc(self) -> str:
        bits = [self.label]
        if self.soc_label:
            bits.append(self.soc_label)
        if self.android_major:
            bits.append("Android %d" % self.android_major)
        if self.build_type:
            bits.append(self.build_type)
        return " · ".join(bits)


@dataclass
class Detection:
    profile: Profile
    score: int = 0
    evidence: list[str] = field(default_factory=list)
    unknown: bool = False
    ambiguous: bool = False       # 命中多台同分机型，不能拿它去刷分区
    candidates: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "key": self.profile.key,
            "label": self.profile.label,
            "score": self.score,
            "unknown": self.unknown,
            "ambiguous": self.ambiguous,
            "candidates": list(self.candidates),
            "evidence": list(self.evidence),
        }
