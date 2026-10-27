"""数字占位符：模型写 {{region.华东.sales_wow}}，渲染时替换为程序算出的数值。

格式按键名后缀决定（金额单位为元、取整数；比例保留 1 位小数）：
  *_wow                                → 环比百分比，带正负号
  *.share / gross_margin / delivery_share / target_rate → 百分比
  *_chg                                → 比率的变动，“+1.2 个百分点”
  *.sales* / *.atv* / target / gross_profit / delivery_sales / impact → 金额（元）
  *.qty* / *.orders* / *.stores        → 整数
"""
from __future__ import annotations

import re
from typing import Any

PH_RE = re.compile(r"\{\{\s*([^{}\s|]+)(?:\|(abs))?\s*\}\}")  # {{键}} 或 {{键|abs}}（跟在“增长/下降”后不带正负号）

PERCENT = ("share", "gross_margin", "delivery_share", "target_rate")
MONEY = ("sales", "atv", "target", "gross_profit", "delivery_sales")


def fmt_value(key: str, v: Any, absolute: bool = False) -> str:
    if v is None:
        return "—"
    leaf = key.rsplit(".", 1)[-1]
    if absolute and isinstance(v, (int, float)):
        v = abs(v)
        if leaf.endswith("_wow"):
            return f"{v * 100:.1f}%"
        if leaf.endswith("_chg"):
            return f"{v * 100:.1f} 个百分点"
        if leaf == "impact":
            return f"{v:,.0f} 元"
    if leaf.endswith("_wow"):
        return f"{v * 100:+.1f}%"
    if leaf.endswith("_chg"):
        return f"{v * 100:+.1f} 个百分点"
    if leaf.startswith(PERCENT):
        return f"{v * 100:.1f}%"
    if leaf == "impact":
        return f"{v:+,.0f} 元"
    if leaf.startswith("atv"):
        return f"{v:.1f} 元"
    if leaf.startswith(MONEY):
        return f"{v:,.0f} 元"
    if leaf.startswith(("qty", "orders")):
        return f"{v:,.0f}"
    if leaf == "stores":
        return f"{int(v)} 家"
    return str(v)


def find_keys(text: str) -> list[str]:
    return [m.group(1) for m in PH_RE.finditer(text)]


def resolve(text: str, flat: dict[str, Any]) -> tuple[str, list[str]]:
    """替换占位符，返回 (文本, 无法解析的键)。"""
    missing: list[str] = []

    def sub(m: re.Match) -> str:
        k = m.group(1)
        if k not in flat:
            missing.append(k)
            return m.group(0)
        return fmt_value(k, flat[k], absolute=bool(m.group(2)))

    out = PH_RE.sub(sub, text)
    out = DUP_UNIT.sub(r"\1", out)  # 占位符已带单位，模型又写了一次（“203,120 元 元”）
    return out, missing


DUP_UNIT = re.compile(r"(\d[\d,.]* (?:元|个百分点)|\d[\d,.]*%)\s*(?:元|个百分点|%)(?![一-龥])")
