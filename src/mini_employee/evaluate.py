"""对比评测：同一批用例在不同模型 / 岗位说明（提示词）/ 配置下各跑一遍，汇总通过率、token、耗时、质检与返工情况。

用法：uv run mini eval evals/example.yaml [--report reports/eval_report.md] [-j 4]

评测文件（YAML）：
  cases: [A01, A02, B01]         # 用例编号；不写则全部（真实模型下自动跳过仅适用于 Mock 的用例）
  jobs: 4                        # 每个方案内并行的进程数
  variants:
    - name: 当前配置              # 不写 env / 改动 = 现在的 .env 与员工配置
    - name: 换一个模型
      env: {MINI_MODEL_NAME: deepseek-chat}          # 环境变量覆盖（模型、地址、密钥变量等）
    - name: 岗位说明改版
      skill_patch:                                    # 在 SKILL.md 副本上做文字替换（old 必须存在）
        - {old: "不超过 5 条", new: "不超过 4 条"}
      employee: {model.temperature: 0.7}             # 修改 employee.yaml 副本中的字段（点号路径）

每个方案在独立目录、独立进程中运行，员工配置的改动只作用于副本，不会改动 employees/ 下的正式配置。
本模块顶层只导入标准库：工作进程须先设置好环境变量（如 MINI_EMPLOYEES），再导入读取这些变量的模块。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any


def _init_env(env: dict[str, str]) -> None:
    os.environ.update(env)


def _eval_one(case: dict[str, Any], workdir: str) -> dict[str, Any]:
    from .runner import _run_one
    r = _run_one(case, Path(workdir))
    r["metrics"] = collect_metrics(Path(workdir))
    return r


def collect_metrics(workdir: Path) -> dict[str, Any]:
    """从用例的任务库与检验记录汇总：token、模型调用、质检轮次、自动修正、退回返工、复核员判定依据不足条数。"""
    m = {"tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "model_calls": 0, "model_ms": 0, "qc_runs": 0,
         "qc_first_pass": 0, "versions": 0, "auto_fix": 0, "rework": 0, "unsupported": 0, "delivered": 0}
    db = workdir / "tasks.db"
    if not db.exists():
        return m
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT task_id, type, payload_json FROM events ORDER BY seq").fetchall()
    finally:
        con.close()
    seen_version: set[tuple[str, Any]] = set()
    version: dict[str, int] = {}
    for tid, typ, raw in rows:
        p = json.loads(raw)
        if typ == "model_called":
            u = p.get("usage") or {}
            m["prompt_tokens"] += u.get("prompt_tokens", 0) or 0
            m["completion_tokens"] += u.get("completion_tokens", 0) or 0
            m["model_calls"] += 1
            m["model_ms"] += p.get("ms", 0) or 0
        elif typ == "state_changed":
            to = p.get("to")
            m["auto_fix"] += to == "AUTO_FIXING"
            m["rework"] += to == "REWORK"
            m["delivered"] += to == "DELIVERED"
            if to == "REWORK":
                version[tid] = version.get(tid, 1) + 1
        elif typ == "qc_run":
            m["qc_runs"] += 1
            key = (tid, version.get(tid, 1))
            if key not in seen_version:  # 每个版本的第一次质检
                seen_version.add(key)
                m["versions"] += 1
                m["qc_first_pass"] += bool(p.get("passed"))
    m["tokens"] = m["prompt_tokens"] + m["completion_tokens"]
    for qc in workdir.glob("*/qc/qc_report.json"):
        rounds = json.loads(qc.read_text(encoding="utf-8")).get("rounds") or []
        if rounds:
            m["unsupported"] += sum(len([d for d in r.get("details", []) if isinstance(d, dict)
                                         and d.get("type") == "cause_unsupported"])
                                    for r in rounds[-1]["results"] if r["rule"] == "cause_review")
    return m


def _slug(name: str, i: int) -> str:
    return f"{i + 1:02d}-" + (re.sub(r"[^\w-]+", "_", name).strip("_")[:30] or "variant")


def _set_path(d: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def prepare_variant(v: dict[str, Any], vdir: Path, employees_dir: Path, employee: str = "weekly_report") -> dict[str, str]:
    """生成方案的环境变量；有提示词 / 配置改动时复制一份员工目录，改动只作用于副本。"""
    import yaml

    env = {k: str(x) for k, x in (v.get("env") or {}).items()}
    if v.get("skill_patch") or v.get("employee"):
        dst = vdir / "employees"
        shutil.copytree(employees_dir, dst)
        edir = dst / employee
        raw = yaml.safe_load((edir / "employee.yaml").read_text(encoding="utf-8"))
        skill_p = edir / raw.get("skill", "SKILL.md")
        text = skill_p.read_text(encoding="utf-8")
        for patch in v.get("skill_patch") or []:
            if patch["old"] not in text:
                raise ValueError(f"方案“{v['name']}”：岗位说明中找不到要替换的文字“{patch['old']}”")
            text = text.replace(patch["old"], patch["new"])
        skill_p.write_text(text, encoding="utf-8")
        for k, x in (v.get("employee") or {}).items():
            _set_path(raw, k, x)
        (edir / "employee.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        env["MINI_EMPLOYEES"] = str(dst)
    return env


def run_eval(spec_path: str, report: str, jobs: int | None = None, only: str | None = None) -> int:
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor

    import yaml

    from .config import EMPLOYEES_DIR, ROOT
    from .runner import load_cases

    sp = Path(spec_path) if Path(spec_path).is_absolute() else ROOT / spec_path
    spec = yaml.safe_load(sp.read_text(encoding="utf-8"))
    variants = spec.get("variants") or [{"name": "当前配置"}]
    ids = spec.get("cases")
    cases = [c for c in load_cases(spec.get("case_paths") or ["cases"])
             if (not ids or c["id"] in ids) and (not only or only in c["id"])]
    jobs = jobs or int(spec.get("jobs", 1))
    (ROOT / "runs").mkdir(exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="eval-", dir=ROOT / "runs"))
    envs = {}
    for i, v in enumerate(variants):  # 先准备全部方案，配置写错时在运行前就报出来
        envs[i] = prepare_variant(v, base / _slug(v["name"], i), EMPLOYEES_DIR)
    results: list[dict[str, Any]] = []
    for i, v in enumerate(variants):
        vdir = base / _slug(v["name"], i)
        print(f"\n== 方案 {i + 1}/{len(variants)}：{v['name']}", flush=True)
        t0 = time.monotonic()
        # 每个方案一个进程池：spawn 启动的新进程先设置方案的环境变量，再导入读取这些变量的模块
        with ProcessPoolExecutor(max_workers=max(1, jobs), mp_context=mp.get_context("spawn"),
                                 initializer=_init_env, initargs=(envs[i],)) as ex:
            rows = list(ex.map(_eval_one, cases, [str(vdir / c["id"]) for c in cases]))
        for r in rows:
            mark = "⏭️" if r["ok"] is None else "✅" if r["ok"] else "❌"
            print(f"  {mark} {r['id']:<5} {r['sec']:>6.1f}s  {r['metrics']['tokens']:>7} tok  {r['msg'][:80]}", flush=True)
        results.append({"variant": v, "rows": rows, "sec": time.monotonic() - t0, "model": _model_name(envs[i])})
    out = Path(report) if Path(report).is_absolute() else ROOT / report
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(results, sp.name, base), encoding="utf-8")
    print(f"\n报告：{out}\n工作目录：{base}")
    return 0


def _model_name(env: dict[str, str]) -> str:
    """方案实际使用的模型（在子进程中解析，避免当前进程已加载的配置影响结果）。"""
    import subprocess
    import sys

    code = ("from mini_employee.config import load_employee; from mini_employee.llm import resolved_config as r;"
            "c = r(load_employee('weekly_report').raw.get('model', {}));"
            "print('mock' if c['provider'] == 'mock' else f\"{c['provider']} / {c.get('model')}\")")
    try:
        p = subprocess.run([sys.executable, "-c", code], env={**os.environ, **env}, capture_output=True, text=True,
                           timeout=60, encoding="utf-8")
        return p.stdout.strip() or "?"
    except Exception:  # noqa: BLE001
        return "?"


def _sum(rows: list[dict[str, Any]], k: str) -> int:
    return sum(r["metrics"][k] for r in rows)


def render_report(results: list[dict[str, Any]], spec_name: str, base: Path) -> str:
    lines = ["# 对比评测报告", "", f"运行时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　评测文件：{spec_name}　工作目录：`{base.name}`", "",
             "| 方案 | 模型 | 通过 | token 合计 | 每条用例 token | 模型调用 | 模型耗时 | 各版本首次质检通过 | 自动修正 | 退回返工 | 复核员：依据不足 | 总用时 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for res in results:
        rows = res["rows"]
        ran = [r for r in rows if r["ok"] is not None]
        ok = sum(bool(r["ok"]) for r in ran)
        tok = _sum(ran, "tokens")
        vers = _sum(ran, "versions")
        lines.append(f"| {res['variant']['name']} | {res['model']} | {ok}/{len(ran)} | {tok:,} | "
                     f"{tok // max(1, len(ran)):,} | {_sum(ran, 'model_calls')} | {_sum(ran, 'model_ms') / 1000:.0f}s | "
                     f"{_sum(ran, 'qc_first_pass')}/{vers} | {_sum(ran, 'auto_fix')} | {_sum(ran, 'rework')} | "
                     f"{_sum(ran, 'unsupported')} | {res['sec']:.0f}s |")
    lines += ["", "方案说明：", ""]
    for res in results:
        v = res["variant"]
        parts = [f"环境变量 {', '.join(f'{k}={x}' for k, x in v['env'].items() if 'KEY' not in k.upper())}"] if v.get("env") else []
        parts += [f"岗位说明“{p['old']}”→“{p['new']}”" for p in v.get("skill_patch") or []]
        parts += [f"配置 {k} = {x}" for k, x in (v.get("employee") or {}).items()]
        lines.append(f"- **{v['name']}**：{'；'.join(parts) or '当前 .env 与员工配置'}")
    names = [res["variant"]["name"] for res in results]
    lines += ["", "## 逐条用例", "", "| 用例 | 名称 | " + " | ".join(names) + " |", "|---|---|" + "---|" * len(names)]
    by_case = [{r["id"]: r for r in res["rows"]} for res in results]
    for c in results[0]["rows"]:
        cells = []
        for m in by_case:
            r = m[c["id"]]
            if r["ok"] is None:
                cells.append("跳过")
                continue
            x = r["metrics"]
            extra = "".join([f"，修正 {x['auto_fix']}" if x["auto_fix"] else "", f"，返工 {x['rework']}" if x["rework"] else "",
                             f"，复核不符 {x['unsupported']}" if x["unsupported"] else ""])
            cells.append(f"{'✅' if r['ok'] else '❌'} {x['tokens'] / 1000:.1f}k tok，{r['sec']:.0f}s{extra}"
                         + ("" if r["ok"] else f"<br>{r['msg'][:120].replace('|', '/')}"))
        lines.append(f"| {c['id']} | {c['name']} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
