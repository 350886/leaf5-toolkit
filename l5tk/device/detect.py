"""机型识别。

唯一输入：getprop 的全量字典。纯函数，不读文件、不碰 adb —— 好测。

判定规则（分数制）：
  model 精确            +40
  model 包含            +25
  device/name 别名命中  +20
  SoC 命中              +15
  Android 大版本命中    +10
  构建类型命中           +5

要求证据来自至少两个不同来源且总分 >= 30。只有 model 一条证据不够 ——
改机型 prop 是常见玩法，但模型名和 SoC 一起改的概率低得多。

两段式命中：
  1. 先比具体型号档案（leaf5 / tab8c / tab8 / tab10）
  2. 具体档案全落空时，才用家族兜底（TabBoox → tab-unknown）
兜底档案永远带上 ambiguous=True —— 它只是「知道这是台 Tab」，
不足以支撑刷分区（Tab10 与 Tab8 的 boot 镜像差 60%）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.errors import DeviceError
from .models import Detection, Profile
from .profiles import GENERIC, PROFILES, TAB_FAMILY

MIN_SCORE = 30
MIN_SOURCES = 2


@dataclass
class _Ctx:
    model: str
    device: str
    name: str
    soc: str
    android: int
    sdk: int
    build_type: str


def _int(v, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _sdk_major(sdk: int) -> int:
    """API 30=A11 31/32=A12 33=A13 34=A14 35=A15。"""
    return {28: 9, 29: 10, 30: 11, 31: 12, 32: 12, 33: 13,
            34: 14, 35: 15}.get(sdk, 0)


def _ctx(props: dict[str, str]) -> _Ctx:
    return _Ctx(
        model=(props.get("ro.product.model") or "").strip(),
        device=(props.get("ro.product.device") or "").strip().lower(),
        name=(props.get("ro.product.name") or "").strip().lower(),
        soc=" ".join(filter(None, (
            props.get("ro.board.platform", ""),
            props.get("ro.hardware", ""),
            props.get("ro.boot.hardware", ""),
            props.get("ro.soc.model", ""),
        ))).lower(),
        android=_int(props.get("ro.build.version.release")),
        sdk=_int(props.get("ro.build.version.sdk")),
        build_type=(props.get("ro.build.type") or "").strip().lower(),
    )


def _score(prof: Profile, c: _Ctx) -> tuple[int, list[str], int]:
    """返回 (分数, 证据, 证据来源数)。"""
    score = 0
    ev: list[str] = []
    sources = 0

    hit_model = 0
    if c.model:
        for rx in prof.models:
            if rx.match(c.model):
                hit_model = 40
                ev.append("model 命中: %s" % c.model)
                break
        if not hit_model and any(rx.search(c.model.lower()) for rx in prof.models):
            hit_model = 25
            ev.append("model 包含: %s" % c.model)
    if hit_model:
        score += hit_model
        sources += 1

    if prof.aliases and (c.device in prof.aliases or c.name in prof.aliases):
        score += 20
        ev.append("device/name 命中: %s" % (c.device or c.name))
        sources += 1

    soc_hit = [k for k in prof.soc_keys if k and k in c.soc]
    if soc_hit:
        score += 15
        ev.append("SoC 命中: %s" % ", ".join(soc_hit))
        sources += 1

    if prof.android_major:
        seen = c.android or _sdk_major(c.sdk)
        if seen == prof.android_major:
            score += 10
            ev.append("Android %s" % seen)
    if prof.build_type and c.build_type == prof.build_type:
        score += 5
        ev.append("构建类型 %s" % c.build_type)

    return score, ev, sources


def _qualifies(score: int, sources: int) -> bool:
    return sources >= MIN_SOURCES and score >= MIN_SCORE


def detect(props: dict[str, str]) -> Detection:
    c = _ctx(props)

    scored: list[tuple[int, Profile, list[str]]] = []
    for prof in PROFILES:
        s, ev, src = _score(prof, c)
        if _qualifies(s, src):
            scored.append((s, prof, ev))

    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[0][0]
        ties = [x for x in scored if x[0] == top]
        if len(ties) > 1:
            # 两台同分：说明字段不足以区分，宁可报歧义也不随便挑一台
            return Detection(
                ties[0][1], top, ties[0][2], ambiguous=True,
                candidates=[p.key for _, p, _ in ties],
            )
        return Detection(scored[0][1], top, scored[0][2])

    # 具体档案落空，试家族兜底（TabBoox 这种同名上报）
    s, ev, src = _score(TAB_FAMILY, c)
    if _qualifies(s, src):
        return Detection(TAB_FAMILY, s, ev, ambiguous=True,
                         candidates=["tab8c", "tab8", "tab10"])

    why = []
    if c.model:
        why.append("model=%s" % c.model)
    if c.soc.strip():
        why.append("soc=%s" % c.soc.strip()[:40])
    return Detection(GENERIC, 0,
                     ["没有机型满足最低证据要求（%s）" % ("，".join(why) or "prop 为空")],
                     unknown=True)


def ensure_known(det: Detection, *, allow_unknown: bool = False) -> Profile:
    """动分区之前过这一关。认不出、以及只认出家族，都停。"""
    if det.unknown and not allow_unknown:
        raise DeviceError(
            "机型未识别，拒绝继续",
            hint="档案表里没有这台。用 `l5tk probe --raw` 导出 prop 后加一条 Profile；"
                 "确认要强行走流程就加 --allow-unknown",
        )
    if det.ambiguous and not allow_unknown:
        raise DeviceError(
            "机型只能确定到家族，拒绝写分区",
            hint="候选：%s。%s" % (", ".join(det.candidates),
                                  det.profile.notes[0] if det.profile.notes else ""),
        )
    return det.profile


def describe(props: dict[str, str]) -> str:
    det = detect(props)
    if det.unknown:
        return "未知机型（%s）" % (props.get("ro.product.model") or "model 为空")
    if det.ambiguous:
        return "%s（歧义：%s）" % (det.profile.label, "/".join(det.candidates))
    return "%s（匹配分 %d）" % (det.profile.label, det.score)
