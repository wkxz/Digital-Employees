"""周报图表（matplotlib，内置思源黑体以正确显示中文）。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

from ..config import ASSETS_DIR  # noqa: E402

logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
_FONT = ASSETS_DIR / "fonts" / "SourceHanSansCN-Regular.otf"  # 思源黑体，SIL OFL 授权
if _FONT.exists():
    font_manager.fontManager.addfont(str(_FONT))
    plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(_FONT)).get_name()
plt.rcParams["axes.unicode_minus"] = False

UP, DOWN, CUR, PREV = "#c0392b", "#1e8449", "#2f5d8a", "#b0b7c3"  # 国内习惯：红涨绿跌

CHART_TITLES = {
    "daily_trend": "本周与上周日销售额对比",
    "region_wow": "各区域销售额环比",
    "category_wow": "各品类销售额环比",
}


def _style(ax) -> None:
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="y", color="#e5e5e5", linewidth=0.8)
    ax.set_axisbelow(True)


def _wan(v: float) -> float:
    return v / 10000


def render(kind: str, kpis: dict[str, Any], out: Path) -> None:
    fig, ax = plt.subplots(figsize=(7.2, 3.4), dpi=150)
    if kind == "daily_trend":
        days = kpis["daily"]
        x = [f"周{d['weekday']}" for d in days]
        ax.plot(x, [_wan(d["sales_prev"]) for d in days], color=PREV, marker="o", label="上周", linewidth=2)
        ax.plot(x, [_wan(d["sales"]) for d in days], color=CUR, marker="o", label="本周", linewidth=2.4)
        ax.set_ylabel("销售额（万元）")
        ax.legend(frameon=False)
    elif kind in ("region_wow", "category_wow"):
        rows = kpis["tables"]["region" if kind == "region_wow" else "category"]
        rows = sorted(rows, key=lambda r: r["sales"], reverse=True)
        names = [f"{r['name']}\n{_wan(r['sales']):.1f} 万元" for r in rows]
        wows = [(r["sales_wow"] or 0) * 100 for r in rows]
        bars = ax.bar(names, wows, color=[UP if w >= 0 else DOWN for w in wows], width=0.55)
        ax.axhline(0, color="#888", linewidth=0.8)
        ax.set_ylabel("环比（%）")
        ax.margins(y=0.22)
        for b, w in zip(bars, wows):
            ax.annotate(f"{w:+.1f}%", (b.get_x() + b.get_width() / 2, w), ha="center",
                        va="bottom" if w >= 0 else "top", fontsize=9, xytext=(0, 3 if w >= 0 else -3),
                        textcoords="offset points")
        ax.tick_params(axis="x", length=0)
    else:
        plt.close(fig)
        raise ValueError(f"未知图表类型：{kind}")
    ax.set_title(CHART_TITLES[kind], fontsize=12, loc="left")
    _style(ax)
    fig.tight_layout()
    fig.savefig(out, format="png")
    plt.close(fig)
