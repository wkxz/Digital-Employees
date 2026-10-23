"""统计周期：采用 ISO 周（周一至周日）。支持“2026-W44”“2026W44”“2026年第44周”。"""
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


_PATTERNS = [r"^(\d{4})-?W(\d{1,2})$", r"^(\d{4})\s*年\s*第\s*(\d{1,2})\s*周$"]


def parse_week(text: str) -> Week:
    s = str(text).strip().upper()
    for p in _PATTERNS:
        m = re.match(p, s)
        if m:
            y, w = int(m.group(1)), int(m.group(2))
            try:
                date.fromisocalendar(y, w, 1)
            except ValueError as e:
                raise ValueError(f"{y} 年没有第 {w} 周") from e
            return Week(y, w)
    raise ValueError(f"统计周期格式应为 2026-W44 或“2026年第44周”，收到：{text}")


def today() -> date:
    """测试与演示可用 MINI_TODAY 固定“今天”。"""
    v = os.environ.get("MINI_TODAY")
    return date.fromisoformat(v) if v else date.today()
