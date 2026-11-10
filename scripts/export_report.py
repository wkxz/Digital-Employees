"""把 docs/技术报告.md 导出为 Word 与 PDF（提交用，要求不超过 8 页）。
- mermaid 架构图换成 docs/architecture.png（由 scripts/draw_architecture.py 生成）；
- 版式取自 docs/report_reference.docx（9.5 号正文、窄页边距）；
- 需要 pandoc；本机装有 Word 时顺带导出 PDF 并打印页数。
运行：uv run python scripts/export_report.py   或   make report"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SRC, DOCX, PDF = DOCS / "技术报告.md", DOCS / "技术报告.docx", DOCS / "技术报告.pdf"


def word_export(docx: Path, pdf: Path) -> int | None:
    """用 Word（COM）另存 PDF 并返回页数；没有 Word 时返回 None。"""
    ps = (f"$w = New-Object -ComObject Word.Application; $w.Visible = $false; "
          f"$d = $w.Documents.Open('{docx}', $false, $true); $n = $d.ComputeStatistics(2); "
          f"$d.SaveAs2('{pdf}', 17); $d.Close($false); $w.Quit(); Write-Output $n")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=180)
        return int(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001  非 Windows / 未装 Word
        return None


def main() -> int:
    if not shutil.which("pandoc"):
        print("需要 pandoc：https://pandoc.org/installing.html")
        return 1
    md = SRC.read_text(encoding="utf-8")
    md = re.sub(r"```mermaid\n.*?```", "![系统架构](architecture.png){width=88%}", md, count=1, flags=re.S)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "report.md"
        src.write_text(md, encoding="utf-8")
        subprocess.run(["pandoc", str(src), "-o", str(DOCX), f"--resource-path={DOCS}",
                        f"--reference-doc={DOCS / 'report_reference.docx'}"], check=True)
    pages = word_export(DOCX, PDF)
    print(f"已导出 {DOCX}" + (f" 与 {PDF}，共 {pages} 页" if pages else "（未找到 Word，未导出 PDF）"))
    if pages and pages > 8:
        print("超过 8 页，需要压缩内容")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
