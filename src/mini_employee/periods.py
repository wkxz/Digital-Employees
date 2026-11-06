"""统计周期：采用 ISO 周（周一至周日）。支持“2026-W44”“2026W44”“2026 W44”“2026年第44周”，
以及完整一周的日期范围“2026-10-26~2026-11-01”（周一至周日）。“第44周”这类不带年份的写法有歧义，不接受。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Week:
    year: int
    week: int

    @property
    def start(self) -> date:
        return date.fromisocalendar(self.year, self.week, 1)

    @property
    def end(self) -> date:
        return self.start + timedelta(days=6)

    @property
    def prev(self) -> "Week":
        y, w, _ = (self.start - timedelta(days=7)).isocalendar()
        return Week(y, w)

    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(7)]

    @property
    def key(self) -> str:
        return f"{self.year}-W{self.week:02d}"

    @property
    def compact(self) -> str:
        return f"{self.year}W{self.week:02d}"

    @property
    def label(self) -> str:
        return f"{self.year} 年第 {self.week} 周（{self.start.month}月{self.start.day}日—{self.end.month}月{self.end.day}日）"


_PATTERNS = [r"^(\d{4})\s*-?\s*W\s*(\d{1,2})$", r"^(\d{4})\s*年\s*第?\s*(\d{1,2})\s*周$"]
_DATE = r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?"
_RANGE = re.compile(rf"^{_DATE}\s*(?:~|～|至|到|—|–|-{{1,2}})\s*{_DATE}$")


def _parse_range(s: str) -> Week | None:
    """日期范围须正好是一个 ISO 周（周一至周日），否则报错说明，不自行猜测。"""
    m = _RANGE.match(s)
    if not m:
        return None
    try:
        a = date(int(m[1]), int(m[2]), int(m[3]))
        b = date(int(m[4]), int(m[5]), int(m[6]))
    except ValueError as e:
        raise ValueError(f"日期不存在：{s}") from e
    if a.isoweekday() != 1 or b != a + timedelta(days=6):
        raise ValueError(f"统计周期须为完整的 ISO 周（周一至周日），{a}～{b} 不是。"
                         f"{a} 所在周为 {a.isocalendar()[0]}-W{a.isocalendar()[1]:02d}")
    y, w, _ = a.isocalendar()
    return Week(y, w)


def parse_week(text: str) -> Week:
    s = str(text).strip().upper()
    rng = _parse_range(s)
    if rng:
        return rng
    for p in _PATTERNS:
        m = re.match(p, s)
        if m:
            y, w = int(m.group(1)), int(m.group(2))
            try:
                date.fromisocalendar(y, w, 1)
            except ValueError as e:
                raise ValueError(f"{y} 年没有第 {w} 周") from e
            return Week(y, w)
    raise ValueError(f"统计周期格式应为 2026-W44、“2026年第44周”或 2026-10-26~2026-11-01，收到：{text}")


def today() -> date:
    """测试与演示可用 MINI_TODAY 固定“今天”。"""
    v = os.environ.get("MINI_TODAY")
    return date.fromisoformat(v) if v else date.today()
