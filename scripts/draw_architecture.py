"""画技术报告的架构图（docs/architecture.png）。与 docs/技术报告.md 中的 mermaid 图内容一致，供导出 Word / PDF 使用。
运行：uv run python scripts/draw_architecture.py"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
font = ROOT / "assets" / "fonts" / "SourceHanSansCN-Regular.otf"
font_manager.fontManager.addfont(str(font))
plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(font)).get_name()

fig, ax = plt.subplots(figsize=(10, 4.6), dpi=200)
ax.set_xlim(0, 100)
ax.set_ylim(0, 46)
ax.axis("off")

INK, MUTED, ACCENT, SOFT, MODEL = "#1f2933", "#52606d", "#2f6fdd", "#eef3fb", "#fff4e5"


def box(x, y, w, h, text, fc="white", ec=INK, size=9.5, bold=False):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.25,rounding_size=1.2", fc=fc, ec=ec, lw=1.1))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=size, color=INK,
            linespacing=1.35)


def arrow(p, q, text="", style="-|>", dashed=False, off=(0, 1.2)):
    ax.add_patch(FancyArrowPatch(p, q, arrowstyle=style, mutation_scale=11, lw=1.0, color=MUTED,
                                 linestyle=(0, (4, 3)) if dashed else "-", shrinkA=2, shrinkB=2))
    if text:
        ax.text((p[0] + q[0]) / 2 + off[0], (p[1] + q[1]) / 2 + off[1], text, ha="center", va="bottom",
                fontsize=8, color=MUTED)


# 程序控制层
ax.add_patch(FancyBboxPatch((14, 13), 50, 30, boxstyle="round,pad=0.3,rounding_size=1.5", fc=SOFT, ec=ACCENT, lw=1.2))
ax.text(16, 40.6, "程序控制层（持有状态、决定下一步、执行闸门）", fontsize=9, color=ACCENT)
box(17, 27, 18, 9, "编排器\n状态机 + 闸门", bold=True)
box(42, 31, 19, 6.5, "步骤执行器")
box(42, 14.5, 19, 6.5, "质检引擎")
box(17, 15, 18, 7.5, "事件存储 SQLite\n只追加")
box(42, 22.5, 19, 6.5, "工具网关\n白名单 · 重试 · 幂等", size=9)

# 输入 / 配置
box(1, 30, 10, 6, "任务表单")
box(1, 3, 22, 6.5, "employee.yaml + SKILL.md\n（员工能力配置）", size=8.5)

# 模型层与工具
ax.add_patch(FancyBboxPatch((70, 29), 28, 13, boxstyle="round,pad=0.3,rounding_size=1.5", fc=MODEL, ec="#d9822b", lw=1.2))
ax.text(72, 39.6, "模型执行层", fontsize=9, color="#b45309")
box(72, 30.5, 24, 7, "Agent 步骤\n轮次 / 时长上限", size=9)
box(72, 13.5, 24, 8, "MCP 工具服务\n7 个工具", bold=True)
box(42, 2.5, 19, 7, "审核台\n通过 / 退回")

arrow((11, 33), (17, 32))
arrow((35, 32.5), (42, 34))
arrow((35, 29), (42, 18))
arrow((26, 27), (26, 22.5))
arrow((51.5, 31), (51.5, 29), off=(0, 0))
arrow((61, 34.2), (72, 34.2), "agent 步骤", off=(-1, 1.2))
arrow((78, 30.5), (78, 21.5), "只能调本步开放的工具", off=(8, -1))
arrow((61, 25.7), (72, 19))
arrow((51.5, 14.5), (51.5, 9.5), off=(0, 0))
arrow((44, 9.5), (34, 27), "通过 / 退回", off=(-4, -8))
arrow((12, 9.5), (20, 27), "配置", dashed=True, off=(-2.5, 0))

ax.text(50, 0.2, "模型只在 agent 步骤内被调用，不能触发状态流转；所有数字由工具算出，模型只用占位符引用", ha="center",
        fontsize=8, color=MUTED)
out = ROOT / "docs" / "architecture.png"
fig.savefig(out, bbox_inches="tight", facecolor="white")
print(out)
