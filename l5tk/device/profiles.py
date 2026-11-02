"""机型档案表。

数据来源分三档，字段 verified 标注：
  * verified=True  —— 在这台机器上跑过命令，值来自 getprop 实测
  * verified=False —— 来自固件包里的内核与 prop 分析，没在真机执行过

**关键坑（V1 现场结论）**：Tab 系列三台的 `ro.product.model` 上报的都是
`TabBoox`，不是 Tab8C/Tab8/Tab10。所以 model 只能把范围收到「Tab 家族」，
具体型号必须靠 ro.product.device / ro.product.name / 固件号区分。
区分不出来时 detect() 返回 ambiguous，刷分区会被拒绝 —— 因为 Tab10 与 Tab8
的 kernel Image 逐字节差 60.33%，刷错就是不开机。
"""

from __future__ import annotations

import re

from .models import Profile

GENERIC = Profile(
    key="generic",
    label="未知机型",
    verified=False,
    notes=("没有匹配到档案表里的任何一台。",
           "此时所有会写分区的操作都应当拒绝执行。"),
)

# 家族级档案：三台 Tab 同名时落到这里。它不是一台真机器，不能用来刷分区。
TAB_FAMILY = Profile(
    key="tab-unknown",
    label="ONYX Tab 系列（型号未定）",
    models=(re.compile(r"^tab\s*boox$", re.I),),
    soc_keys=("bengal", "scuba", "sm6115"),
    soc_label="BENGAL + SCUBA (SM6115)",
    family="tab",
    android_major=11,
    build_type="userdebug",
    ab_slot=False,
    boot_part="boot",
    verified=False,
    notes=("ro.product.model 只报了 TabBoox，分不出是 8C / 8 / 10。",
           "三台的 boot 镜像不可互换（Tab8C 与 Tab10 的 Image 差 60.33%）。",
           "要刷机必须先确认具体型号：设置里看设备信息，或用 `l5tk probe --raw` 看 device 字段。"),
)

_TAB_NOTES = ("model 报的是 TabBoox（三台同名），本档案靠 device/name 字段区分。",
              "userdebug，优先试 adb root；9008 支路因 SoC 不同走不通。")

PROFILES: tuple[Profile, ...] = (
    Profile(
        key="leaf5",
        label="ONYX BOOX Leaf5",
        models=(re.compile(r"^leaf\s*5$", re.I), re.compile(r"^boox\s*leaf\s*5$", re.I)),
        aliases=("leaf5", "boox_leaf5"),
        soc_keys=("lito", "lagoon", "sm6350"),
        soc_label="LITO + LAGOON (SM6350)",
        family="leaf",
        android_major=13,
        build_type="user",
        ab_slot=True,
        boot_part="boot",
        verified=True,
        notes=("实测：固件 2026-09-28_15-24_4.2.3-rel_0928_656b72278，内核 #38 4.19.157-perf。",
               "A/B 双槽（有 boot_a / boot_b），实际刷哪个由分区表 + ro.boot.slot_suffix 决定。",
               "已解锁（ro.boot.flash.locked=0，verifiedboot=orange），Magisk 31.1 在位。"),
    ),
    Profile(
        key="tab8c",
        label="ONYX BOOX Tab8C",
        models=(re.compile(r"^tab\s*8\s*c$", re.I), re.compile(r"^boox\s*tab\s*8\s*c$", re.I)),
        aliases=("tab8c", "boox_tab8c"),
        soc_keys=("bengal", "scuba", "sm6115"),
        soc_label="BENGAL + SCUBA (SM6115)",
        family="tab",
        android_major=11,
        build_type="userdebug",
        ab_slot=False,
        boot_part="boot",
        verified=False,
        notes=("与 Tab8 同一内核 commit，Image 逐字节差异 1.09%。",) + _TAB_NOTES,
    ),
    Profile(
        key="tab8",
        label="ONYX BOOX Tab8",
        models=(re.compile(r"^tab\s*8$", re.I), re.compile(r"^boox\s*tab\s*8$", re.I)),
        aliases=("tab8", "boox_tab8"),
        soc_keys=("bengal", "scuba", "sm6115"),
        soc_label="BENGAL + SCUBA (SM6115)",
        family="tab",
        android_major=11,
        build_type="userdebug",
        ab_slot=False,
        boot_part="boot",
        verified=False,
        notes=("与 Tab8C 同源，一份 boot 补丁大概率两台通用。",) + _TAB_NOTES,
    ),
    Profile(
        key="tab10",
        label="ONYX BOOX Tab10",
        models=(re.compile(r"^tab\s*10$", re.I), re.compile(r"^boox\s*tab\s*10$", re.I)),
        aliases=("tab10", "boox_tab10"),
        soc_keys=("bengal", "scuba", "sm6115"),
        soc_label="BENGAL + SCUBA (SM6115)",
        family="tab",
        android_major=11,
        build_type="userdebug",
        ab_slot=False,
        boot_part="boot",
        verified=False,
        notes=("SoC 与 Tab8C 相同，但 Image 差异 60.33%，补丁不能通用。",) + _TAB_NOTES,
    ),
)


def by_key(key: str) -> Profile:
    for p in PROFILES + (TAB_FAMILY,):
        if p.key == key.lower():
            return p
    return GENERIC


def all_profiles() -> tuple[Profile, ...]:
    return PROFILES


def table() -> str:
    """命令行 `l5tk profiles` 打这张表。"""
    rows = ["%-11s %-22s %-28s %-8s %-9s %s"
            % ("KEY", "机型", "SoC", "Android", "构建", "实测")]
    for p in PROFILES:
        rows.append("%-11s %-22s %-28s %-8s %-9s %s"
                    % (p.key, p.label, p.soc_label or "-",
                       p.android_major or "-", p.build_type or "-",
                       "是" if p.verified else "否"))
    rows.append("%-11s %-22s %-28s %-8s %-9s %s"
                % (TAB_FAMILY.key, TAB_FAMILY.label, TAB_FAMILY.soc_label,
                   TAB_FAMILY.android_major, TAB_FAMILY.build_type,
                   "—（兜底，不可刷机）"))
    return "\n".join(rows)
