"""执行结果的统一形状。

为什么不用裸 tuple：V1 里到处是 (ok, bytes, rc) 三件套，调用点经常忘了看 rc，
于是「命令失败」被当成「输出为空」。这里强制带 rc 与耗时，print 时有固定格式。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExecResult:
    argv: tuple[str, ...]
    rc: int
    out: str = ""
    err: str = ""
    ms: int = 0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out

    @property
    def text(self) -> str:
        """合并输出，用于 grep 型判断（很多 adb 子命令把信息写到 stderr）。"""
        return (self.out + "\n" + self.err).strip()

    def line(self) -> str:
        return "$ %s  -> rc=%d %dms%s" % (
            " ".join(self.argv), self.rc, self.ms, " (timeout)" if self.timed_out else "")

    def require(self, what: str = "") -> "ExecResult":
        """失败就抛，成功返回自己。省掉每个调用点写 if。"""
        if not self.ok:
            from .errors import AdbError
            raise AdbError(
                "%s 失败（rc=%d）%s" % (what or self.argv[0], self.rc,
                                       "，超时" if self.timed_out else ""),
                hint=(self.err or self.out).strip()[:400],
            )
        return self

    def __str__(self) -> str:
        return self.line()


@dataclass
class Step:
    """流程里的一步，供 GUI 画进度条与 CLI 打表。"""
    key: str
    title: str
    ok: bool | None = None       # None = 未执行
    detail: str = ""
    seconds: float = 0.0
    meta: dict = field(default_factory=dict)

    def done(self, detail: str = "", ok: bool = True, **meta) -> "Step":
        self.ok = ok
        self.detail = detail
        self.meta.update(meta)
        return self


@dataclass
class Outcome:
    """一次操作的最终结论。ok=False 时 reason 必须能让人看懂下一步做什么。"""
    ok: bool
    stage: str
    detail: str = ""
    reasons: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "stage": self.stage, "detail": self.detail,
                "reasons": list(self.reasons), "data": dict(self.data)}
