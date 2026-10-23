"""数字占位符：模型写 {{region.华东.sales_wow}}，渲染时替换为程序算出的数值。

格式按键名后缀决定：
  *_wow / *.share   → 百分比（环比带正负号）
  *.sales* / *.atv* → 金额（≥1 万显示“万元”）
  *.qty* / *.orders* / *.stores → 整数
"""
from __future__ import annotations

import re
from typing import Any

PH_RE = re.compile(r"\{\{\s*([^{}\s|]+)(?:\|(abs))?\s*\}\}")  # {{键}} 或 {{键|abs}}（跟在“增长/下降”后不带正负号）


def fmt_value(key: str, v: Any, absolute: bool = False) -> str:
    if v is None:
        return "—"
    if absolute and isinstance(v, (int, float)):
        v = abs(v)
        if key.rsplit(".", 1)[-1].endswith("_wow"):
            return f"{v * 100:.1f}%"
    leaf = key.rsplit(".", 1)[-1]
    if leaf.endswith("_wow"):
        return f"{v * 100:+.1f}%"
    if leaf == "share":
        return f"{v * 100:.1f}%"
    if leaf == "impact":
        return f"{v / 10000:+,.1f} 万元"
    if leaf.startswith(("sales", "atv")):
        if leaf.startswith("atv"):
            return f"{v:.1f} 元"
        return f"{v / 10000:,.1f} 万元" if abs(v) >= 10000 else f"{v:,.0f} 元"
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

    return PH_RE.sub(sub, text), missing
