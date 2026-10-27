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
    "weekly_trend": "近 4 周实收趋势",
    "region_target": "各区域本周实收与目标达成率",
}


def _style(ax) -> None:
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="y", color="#e5e5e5", linewidth=0.8)
    ax.set_axisbelow(True)


def _yuan(ax) -> None:
    from matplotlib.ticker import FuncFormatter
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))


HALF_WIDTH = {"weekly_trend", "region_target"}  # 适合两张并排的图：画幅窄、字号大，缩到半页宽仍清晰


def render(kind: str, kpis: dict[str, Any], out: Path) -> None:
    half = kind in HALF_WIDTH
    fig, ax = plt.subplots(figsize=(4.6, 3.0) if half else (7.2, 3.4), dpi=180 if half else 150)
    if half:
        ax.tick_params(labelsize=10)
    if kind == "daily_trend":
        days = kpis["daily"]
        x = [f"周{d['weekday']}" for d in days]
        ax.plot(x, [d["sales_prev"] for d in days], color=PREV, marker="o", label="上周", linewidth=2)
        ax.plot(x, [d["sales"] for d in days], color=CUR, marker="o", label="本周", linewidth=2.4)
        ax.set_ylabel("销售额（元）")
        _yuan(ax)
        ax.legend(frameon=False)
    elif kind == "weekly_trend":
        weeks = kpis.get("weekly") or []
        x = [w["label"] for w in weeks]
        bars = ax.bar(x, [w["sales"] for w in weeks], color=[PREV] * (len(weeks) - 1) + [CUR], width=0.55, label="实收")
        for b, w in zip(bars, weeks):
            ax.annotate(f"{w['sales']:,.0f}", (b.get_x() + b.get_width() / 2, w["sales"]), ha="center", va="bottom",
                        fontsize=9, xytext=(0, 3), textcoords="offset points")
        if all(w.get("target") for w in weeks):
            ax.plot(x, [w["target"] for w in weeks], color="#b05a00", marker="o", linestyle="--", label="目标")
            ax.legend(frameon=False, loc="lower right")
        ax.set_ylabel("实收（元）")
        _yuan(ax)
        ax.margins(y=0.15)
        ax.tick_params(axis="x", length=0)
    elif kind == "region_target":
        rows = sorted(kpis["tables"]["region"], key=lambda r: r["sales"], reverse=True)
        x = [r["name"] for r in rows]
        has_target = all(r.get("target") for r in rows)
        if has_target:
            ax.bar([i - 0.2 for i in range(len(x))], [r["target"] for r in rows], width=0.4, color=PREV, label="目标")
        bars = ax.bar([i + (0.2 if has_target else 0) for i in range(len(x))], [r["sales"] for r in rows],
                      width=0.4 if has_target else 0.55, color=CUR, label="实收")
        for b, r in zip(bars, rows):
            note = f"{r['target_rate'] * 100:.1f}%" if has_target and r.get("target_rate") is not None \
                else f"{(r['sales_wow'] or 0) * 100:+.1f}%"
            ax.annotate(note, (b.get_x() + b.get_width() / 2, r["sales"]), ha="center", va="bottom", fontsize=9,
                        xytext=(0, 3), textcoords="offset points")
        ax.set_xticks(range(len(x)), x)
        ax.set_ylabel("实收（元）")
        _yuan(ax)
        ax.margins(y=0.15)
        ax.legend(frameon=False, loc="upper right")
        ax.tick_params(axis="x", length=0)
    elif kind in ("region_wow", "category_wow"):
        rows = kpis["tables"]["region" if kind == "region_wow" else "category"]
        rows = sorted(rows, key=lambda r: r["sales"], reverse=True)
        names = [f"{r['name']}\n{r['sales']:,.0f} 元" for r in rows]
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
    ax.set_title(CHART_TITLES[kind], fontsize=12.5 if half else 12, loc="left")
    _style(ax)
    fig.tight_layout()
    fig.savefig(out, format="png")
    plt.close(fig)
