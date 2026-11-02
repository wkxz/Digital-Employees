/* 演示数据：仅在后端未连接（直接双击打开 index.html）或地址带 ?demo 时启用，页面底部会明确标注“演示数据”。
   数据结构与 app/web_server.py 返回的完全一致，便于离线检查界面。数字取自官方公开样例 A-01（第 44 周）。 */
(function () {
  "use strict";
  const pad = (n, l = 2) => String(n).padStart(l, "0");
  const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
  let clock = null;
  const ts = () => iso(clock ? new Date(clock) : new Date());
  const tickClock = (ms) => { if (clock) clock += ms; };
  const sleep = (ms, instant) => (instant ? (tickClock(ms + Math.random() * ms * 0.4), Promise.resolve()) : new Promise((r) => setTimeout(r, ms)));
  const hex = (n) => Array.from({ length: n }, () => "0123456789abcdef"[(Math.random() * 16) | 0]).join("");
  let SEQ = 0;

  const LABELS = { CREATED: "已创建", ADMISSION: "准入校验", WAITING_INPUT: "待补材料", RUNNING: "执行中", QC: "自动质检", AUTO_FIXING: "自动修正", SUSPENDED: "暂停转人工", PENDING_REVIEW: "待审核", REWORK: "返工中", DELIVERED: "已交付", REJECTED: "已拒绝", CANCELLED: "已取消" };
  const TERMINAL = ["DELIVERED", "REJECTED", "CANCELLED"];
  const T = (src, event, dst, actors, guard, desc) => ({ src, event, dst, actors, guard, desc });
  const TRANSITIONS = [
    T("CREATED", "submit", "ADMISSION", ["system"], true, "表单 Schema 校验通过"),
    T("CREATED", "form_invalid", "REJECTED", ["system"], false, "表单字段缺失或格式错误（如周期在未来、文件类型不支持）"),
    T("ADMISSION", "admission_pass", "RUNNING", ["system"], false, "必需文件齐全，列、周次覆盖、数据质量合格"),
    T("ADMISSION", "admission_missing", "WAITING_INPUT", ["system"], false, "缺文件 / 缺列 / 缺上周数据（记录缺什么）"),
    T("ADMISSION", "admission_invalid", "REJECTED", ["system"], false, "输入不合规（含个人隐私等）"),
    T("ADMISSION", "step_failed", "SUSPENDED", ["system"], false, "校验用的工具重试耗尽（保留断点）"),
    T("WAITING_INPUT", "supplement", "ADMISSION", ["initiator"], true, "发起人上传补充材料，重新准入"),
    T("RUNNING", "steps_done", "QC", ["system"], false, "全部步骤完成"),
    T("RUNNING", "step_missing_input", "WAITING_INPUT", ["system"], false, "执行中发现缺材料（保留断点）"),
    T("RUNNING", "step_failed", "SUSPENDED", ["system"], false, "工具重试耗尽 / 超轮次 / 超时（保留断点）"),
    T("QC", "qc_pass", "PENDING_REVIEW", ["system"], true, "检验记录中全部 blocking 规则通过"),
    T("QC", "qc_fail", "AUTO_FIXING", ["system"], true, "有 blocking 规则不通过，修正次数 < 上限"),
    T("QC", "qc_fail_exhausted", "SUSPENDED", ["system"], true, "修正次数 ≥ 上限，转人工"),
    T("QC", "step_failed", "SUSPENDED", ["system"], false, "质检程序异常（保留断点）"),
    T("AUTO_FIXING", "fix_done", "QC", ["system"], false, "修正后重新质检"),
    T("AUTO_FIXING", "step_failed", "SUSPENDED", ["system"], false, "修正过程中工具/模型失败或超限"),
    T("PENDING_REVIEW", "approve", "DELIVERED", ["reviewer"], true, "审核人通过，且审核版本 = 交付版本、质检通过"),
    T("PENDING_REVIEW", "request_changes", "REWORK", ["reviewer"], true, "审核人退回，意见非空，返工次数 < 上限"),
    T("PENDING_REVIEW", "request_changes_exhausted", "SUSPENDED", ["reviewer"], true, "返工次数已达上限，转线下沟通"),
    T("REWORK", "rework_done", "QC", ["system"], false, "按意见重做完成，新版本重新质检"),
    T("REWORK", "step_missing_input", "WAITING_INPUT", ["system"], false, "重做中发现缺材料（保留断点）"),
    T("REWORK", "step_failed", "SUSPENDED", ["system"], false, "重做中工具/模型失败或超限（保留断点）"),
    T("SUSPENDED", "retry", null, ["initiator", "reviewer"], true, "人工确认后从断点继续（回到暂停前的状态与步骤）"),
    T("SUSPENDED", "manual_recheck", "QC", ["initiator", "reviewer"], false, "人工补充/修改后重新质检（仍须质检通过才能送审）"),
  ].concat(Object.keys(LABELS).filter((s) => !TERMINAL.includes(s)).map((s) => T(s, "cancel", "CANCELLED", ["initiator"], false, "发起人取消")));

  const STEPS = [
    { id: "load", name: "读取销售明细", type: "tool", tools: ["read_sales_data"] },
    { id: "kpi", name: "计算指标与识别异常", type: "tool", tools: ["compute_kpis"] },
    { id: "minutes", name: "读取周会纪要", type: "tool", tools: ["read_minutes"] },
    { id: "charts", name: "生成图表", type: "tool", tools: ["render_charts"] },
    { id: "analysis", name: "异常原因分析", type: "agent", tools: ["search_minutes", "query_kpi"], per_version: true },
    { id: "draft", name: "撰写周报", type: "agent", tools: ["query_kpi"], per_version: true },
    { id: "render", name: "生成交付文件", type: "render", tools: ["generate_report"], per_version: true },
  ];
  const FAULTS = [
    { id: "search_minutes:timeout", tool: "search_minutes", mode: "timeout", label: "检索服务超时", scope: "both" },
    { id: "read_minutes:timeout", tool: "read_minutes", mode: "timeout", label: "纪要读取服务超时", scope: "both" },
    { id: "query_kpi:error", tool: "query_kpi", mode: "error", label: "KPI 查询服务 503", scope: "global" },
    { id: "llm:timeout", tool: "llm", mode: "timeout", label: "模型接口断开", scope: "both" },
    { id: "mock:direction_error", tool: "mock", mode: "direction_error", label: "Mock：涨跌写反", scope: "task" },
    { id: "mock:hardcode_number", tool: "mock", mode: "hardcode_number", label: "Mock：硬编码数字", scope: "task" },
    { id: "mock:forbidden_tool", tool: "mock", mode: "forbidden_tool", label: "Mock：越权调用工具", scope: "task" },
    { id: "mock:loop_tools", tool: "mock", mode: "loop_tools", label: "Mock：循环调用不收尾", scope: "task" },
  ];
  const META = {
    model: "mock", runs_dir: "（演示数据，未连接后端）", states: LABELS, terminal: TERMINAL, transitions: TRANSITIONS, faults: FAULTS,
    roles: { initiator: "发起人", reviewer: "审核人", system: "系统" }, sample_available: true, config_error: null,
    employee: {
      id: "weekly_report", name: "经营周报员工",
      description: "读取门店销售明细与周会纪要，汇总实收、毛利率、外卖占比和目标达成率，解释异常波动，生成带图表和结论的经营周报（Word）。",
      form: [
        { key: "period", label: "统计周期", type: "iso_week", required: true, help: "如 2026-W44 或 2026年第44周" },
        { key: "sales_file", label: "销售明细", type: "file", ext: ["xlsx", "csv"], required: true, multiple: true },
        { key: "minutes_file", label: "周会纪要", type: "file", ext: ["docx", "md"], required: false, help: "建议上传：异常原因优先引用纪要；未上传时照常生成，原因均标注“待核实”" },
        { key: "master_file", label: "主数据", type: "file", ext: ["xlsx"], required: false, help: "可选：门店信息、商品信息、销售目标工作表（可只含其中几张）。门店新开或目标调整时上传，优先于系统自带的主数据" },
        { key: "focus_region", label: "重点关注区域", type: "enum", options: ["华东", "华南", "华北", "西南"], required: false, guide: "在“核心指标”中单独写一句该区域的实收、环比和目标达成率", section: "overall" },
      ],
      steps: STEPS.map(({ id, name, type, tools }) => ({ id, name, type, tools })),
      tools: ["read_sales_data", "compute_kpis", "read_minutes", "search_minutes", "query_kpi", "render_charts", "generate_report"],
      limits: { max_model_turns_per_step: 8, max_step_seconds: 180, max_task_seconds: 600, tool_max_retries: 3, tool_backoff_seconds: [1, 2, 4], tool_timeout_seconds: 30, qc_max_fix_rounds: 2, rework_max_rounds: 3 },
      quality_checks: [
        { rule: "structure", name: "结构完整" }, { rule: "data_consistency", name: "数据一致性" }, { rule: "cause_evidence", name: "异常归因有据" },
        { rule: "threshold_coverage", name: "波动对象逐一列出" }, { rule: "minutes_vs_data", name: "纪要说法与数据核对" }, { rule: "keyword_required", name: "摘要讲清核心指标" },
        { rule: "regex_forbidden", name: "不写数据不支持的指标" }, { rule: "sensitive_words", name: "敏感信息" }, { rule: "length", name: "字数" }, { rule: "page_limit", name: "篇幅" },
      ],
    },
  };

  /* ------------------------------------------------------------ 第 44 周指标（官方样例 A-01） */
  function kpisFor(week) {
    const k = week === 44 ? 1 : 0.92 + ((week * 37) % 17) / 100;
    const s = (v) => Math.round(v * k);
    const regions = [
      { name: "华东", sales: s(197618), sales_prev: s(224516), sales_wow: -0.120, gross_margin: 0.612, delivery_share: 0.301, target: 234980, target_rate: 0.841 * k },
      { name: "华南", sales: s(177306), sales_prev: s(203120), sales_wow: -0.127, gross_margin: 0.598, delivery_share: 0.352, target: 192097, target_rate: 0.923 * k },
      { name: "华北", sales: s(119712), sales_prev: s(123734), sales_wow: -0.033, gross_margin: 0.621, delivery_share: 0.297, target: 129980, target_rate: 0.921 * k },
      { name: "西南", sales: s(78998), sales_prev: s(81116), sales_wow: -0.026, gross_margin: 0.618, delivery_share: 0.311, target: 82033, target_rate: 0.963 * k },
    ];
    const total = regions.reduce((a, r) => a + r.sales, 0);
    return {
      period: `2026-W${week}`, period_label: `2026 年第 ${week} 周`, threshold: 0.1,
      total: { sales: week === 44 ? 573635 : total, sales_prev: s(632486), sales_wow: -0.093, gross_margin: 0.611, delivery_share: 0.318, target: 639090, target_rate: total / 639090, stores: 14 },
      regions,
      categories: [
        { name: "饮品", sales: total * 0.31, sales_wow: -0.142, share: 0.31, gross_margin: 0.68 },
        { name: "蛋糕", sales: total * 0.26, sales_wow: -0.061, share: 0.26, gross_margin: 0.58 },
        { name: "面包", sales: total * 0.22, sales_wow: -0.074, share: 0.22, gross_margin: 0.6 },
        { name: "西点", sales: total * 0.13, sales_wow: -0.083, share: 0.13, gross_margin: 0.57 },
        { name: "预包装食品", sales: total * 0.08, sales_wow: 0.021, share: 0.08, gross_margin: 0.45 },
      ],
      stores: [],
      weekly: [
        { week: "2026-W41", label: "W41", sales: 686193, target: 640000 }, { week: "2026-W42", label: "W42", sales: 605909, target: 640000 },
        { week: "2026-W43", label: "W43", sales: 632486, target: 640000 }, { week: "2026-W44", label: "W44", sales: total, target: 639090 },
      ],
      daily: ["一", "二", "三", "四", "五", "六", "日"].map((d, i) => ({ date: `2026-10-${26 + i > 31 ? "0" + (26 + i - 31) : 26 + i}`, weekday: d, sales: total / 7 * [0.86, 0.84, 0.87, 0.95, 1.06, 1.22, 1.2][i], sales_prev: 632486 / 7 * [0.9, 0.93, 0.95, 0.98, 1.05, 1.11, 1.08][i] })),
      top_items: ["拿铁", "生椰拿铁", "美式咖啡", "6寸生日蛋糕", "草莓奶油切块"],
      anomalies: [
        { id: "A1", label: "华东区", dimension_label: "区域", direction: "down", sales: s(197618), sales_prev: s(224516), wow: -0.12, impact: -26898, drivers: [{ label: "杭州湖滨店（HZ01）", wow: -0.383 }, { label: "上海静安店（SH02）", wow: -0.179 }] },
        { id: "A2", label: "华南区", dimension_label: "区域", direction: "down", sales: s(177306), sales_prev: s(203120), wow: -0.127, impact: -25814, drivers: [{ label: "深圳南山店（SZ01）", wow: -0.153 }, { label: "深圳福田店（SZ02）", wow: -0.173 }] },
      ],
    };
  }

  /* ------------------------------------------------------------ 任务模拟 */
  const tasks = [];
  let globalFaults = [];
  const weekOf = (p) => { const m = String(p || "").match(/(\d{4})\D*W?\s*第?\s*(\d{1,2})/i); return m ? +m[2] : null; };

  function newTask(form, files, faults, created) {
    const id = "T" + iso(new Date(created || Date.now())).slice(5, 10).replace("-", "") + "-" + iso(new Date(created || Date.now())).slice(11, 19).replace(/:/g, "") + "-" + hex(4);
    const t = {
      id, state: "CREATED", version: 1, form, file_names: files, faults: faults || [], pause_reason: null, resume_to: null,
      created_at: ts(), updated_at: ts(), qc_rounds: 0, rework_rounds: 0, artifact: null, events: [], done: new Set(),
      qcRecords: [], needFix: true, feedback: {},
    };
    tasks.unshift(t);
    ev(t, "task_created", null, { employee: "weekly_report", form });
    return t;
  }
  function ev(t, type, step, payload) { t.events.push({ seq: ++SEQ, ts: ts(), type, step: step || null, payload }); t.updated_at = ts(); }
  function go(t, event, to, actor = "system", reason = null, extra = {}) {
    ev(t, "state_changed", null, { from: t.state, to, event, actor, reason });
    t.state = to;
    if (to !== "SUSPENDED" && to !== "WAITING_INPUT") t.pause_reason = null;
    Object.assign(t, extra);
  }
  const hasFault = (t, tool) => t.faults.concat(globalFaults).some((f) => f.tool === tool);
  const stepKey = (t, s) => (s.per_version ? `${s.id}@v${t.version}` : s.id);

  async function drive(t, instant) {
    for (let guard = 0; guard < 40; guard++) {
      if (TERMINAL.includes(t.state)) return;
      const st = t.state;
      if (st === "CREATED") {
        await sleep(350, instant);
        if (!weekOf(t.form.period)) {
          const r = `统计周期格式应为 2026-W44 或“2026年第44周”，收到：${t.form.period || "（空）"}`;
          go(t, "form_invalid", "REJECTED", "system", r, { pause_reason: r });
          return;
        }
        ev(t, "form_validated", null, { errors: [] });
        go(t, "submit", "ADMISSION", "system", "表单校验通过");
      } else if (st === "ADMISSION") {
        await sleep(500, instant);
        const w = weekOf(t.form.period);
        if (t.file_names.sales_file) ev(t, "tool_called", "admission", { tool: "read_sales_data", attempt: 1, ms: 412 });
        if (t.file_names.minutes_file) {
          if (hasFault(t, "read_minutes")) {
            for (let a = 1; a <= 3; a++) { await sleep(300, instant); ev(t, "tool_failed", "admission", { tool: "read_minutes", attempt: a, ms: 30000, kind: "RETRYABLE", error: "纪要读取服务超时（30s）" }); }
            const r = "准入校验时读取周会纪要失败：纪要读取服务超时，已重试 3 次。请确认服务恢复后点击重试，将从准入校验继续。";
            go(t, "step_failed", "SUSPENDED", "system", r, { pause_reason: r, resume_to: "ADMISSION" });
            return;
          }
          ev(t, "tool_called", "admission", { tool: "read_minutes", attempt: 1, ms: 96 });
        }
        const problems = [];
        if (!t.file_names.sales_file) problems.push("缺少材料：销售明细，请上传");
        if (t.file_names.sales_file && w > 44) problems.push(`销售数据实际覆盖 2026-08-31 至 2026-11-01，不包含统计周期 2026-W${w}，无法生成该周周报。请上传包含该周的销售数据，或改选数据范围内的周期`);
        if (t.file_names.sales_file && w === 36) problems.push("缺少上一周（2026-W35，2026-08-24~2026-08-30）的销售数据，无法计算环比。请补充上传（可只上传上周数据，系统会与已上传的数据自动合并）");
        ev(t, "admission_checked", "admission", { problems, sales: { rows: 45213, date_range: ["2026-08-31", "2026-11-01"] } });
        if (problems.length) {
          const r = problems.join("；") + "。补充后将继续执行。";
          go(t, "admission_missing", "WAITING_INPUT", "system", r, { pause_reason: r, resume_to: "RUNNING" });
          return;
        }
        go(t, "admission_pass", "RUNNING", "system", "准入校验通过");
      } else if (st === "RUNNING" || st === "REWORK") {
        const list = st === "REWORK" ? STEPS.filter((s) => s.per_version) : STEPS;
        for (let i = 0; i < list.length; i++) {
          const s = list[i];
          const key = stepKey(t, s);
          if (t.done.has(key)) { ev(t, "step_skipped", s.id, { key, reason: "已完成，断点恢复跳过" }); continue; }
          ev(t, "step_started", s.id, { key, name: s.name, index: STEPS.indexOf(s) + 1, version: t.version });
          await sleep(s.type === "agent" ? 1300 : 650, instant);
          if (s.type === "agent") {
            if (hasFault(t, "llm")) {
              for (let a = 1; a <= 3; a++) { await sleep(250, instant); ev(t, "tool_failed", s.id, { tool: "llm", attempt: a, ms: 60000, kind: "RETRYABLE", error: "模型接口不可用：Connection error" }); }
              const r = `第 ${STEPS.indexOf(s) + 1} 步「${s.name}」失败：模型接口不可用（已重试 3 次）。已完成 ${t.done.size}/7 步。请确认网络恢复后点击重试，将从失败的步骤继续。`;
              go(t, "step_failed", "SUSPENDED", "system", r, { pause_reason: r, resume_to: st });
              return;
            }
            if (s.id === "analysis") {
              if (hasFault(t, "search_minutes")) {
                ev(t, "model_called", s.id, { model: "mock", purpose: "analysis", turn: 1, ms: 820, usage: { total_tokens: 3120 }, tool_calls: [{ name: "search_minutes", args: { keywords: ["华东", "下滑"] } }] });
                for (let a = 1; a <= 3; a++) { await sleep(300, instant); ev(t, "tool_failed", s.id, { tool: "search_minutes", attempt: a, ms: 30000, kind: "RETRYABLE", error: "检索服务超时（30s）" }); }
                const r = `第 5 步「异常原因分析」失败：检索服务不可用（search_minutes 已重试 3 次）。已完成 4/7 步。请检查服务后点击重试，将从失败的步骤继续，不会重跑已完成的步骤。`;
                go(t, "step_failed", "SUSPENDED", "system", r, { pause_reason: r, resume_to: st });
                return;
              }
              ev(t, "model_called", s.id, { model: "mock", purpose: "analysis", turn: 1, ms: 1180, usage: { total_tokens: 5260 }, tool_calls: [{ name: "query_kpi", args: { prefix: "region.华东" } }] });
              ev(t, "tool_called", s.id, { tool: "query_kpi", attempt: 1, ms: 8 });
              ev(t, "model_called", s.id, { model: "mock", purpose: "analysis", turn: 2, ms: 1460, usage: { total_tokens: 6020 }, tool_calls: [] });
            } else {
              ev(t, "model_called", s.id, { model: "mock", purpose: "draft", turn: 1, ms: 2210, usage: { total_tokens: 7380 }, tool_calls: [] });
            }
          } else {
            s.tools.forEach((tool) => ev(t, "tool_called", s.id, { tool, attempt: 1, ms: 40 + ((Math.random() * 600) | 0) }));
          }
          if (s.id === "render") {
            const tag = `v${t.version}`;
            t.artifact = { tag, sha256: hex(64), primary: `work/${tag}/经营周报_2026W${weekOf(t.form.period)}.docx`, fix_actions: [] };
          }
          t.done.add(key);
          ev(t, "step_completed", s.id, { key, version: t.version, sha256: hex(64), ms: 600 });
        }
        if (st === "REWORK") go(t, "rework_done", "QC", "system", "按意见重做完成");
        else go(t, "steps_done", "QC", "system", "全部步骤完成");
      } else if (st === "QC") {
        await sleep(700, instant);
        const fail = t.needFix && t.version === 1;
        const rec = qcRecord(t, fail);
        t.qcRecords.push(rec);
        ev(t, "qc_run", null, { round_no: rec.round_no, passed: rec.passed, failed: rec.results.filter((r) => !r.passed).map((r) => r.rule), blocking_failed: rec.results.filter((r) => !r.passed && r.severity === "blocking").map((r) => r.rule) });
        if (fail) { go(t, "qc_fail", "AUTO_FIXING", "system", "数据一致性未通过"); }
        else { go(t, "qc_pass", "PENDING_REVIEW", "system", "质检全部通过"); return; }
      } else if (st === "AUTO_FIXING") {
        const n = t.qc_rounds + 1;
        ev(t, "step_started", "fix", { key: `fix@v${t.version}#${n}`, name: `自动修正第 ${n} 轮`, version: t.version });
        await sleep(900, instant);
        t.artifact = { ...t.artifact, tag: `v${t.version}f${n}`, sha256: hex(64), fix_actions: ["“环比 +12%”能对上 region.华东.sales_wow，程序替换为占位符", "涨跌用词与环比方向不一致 1 处，交模型改写"] };
        ev(t, "step_completed", "fix", { key: `fix@v${t.version}#${n}`, version: t.version, sha256: t.artifact.sha256, actions: t.artifact.fix_actions });
        t.needFix = false;
        go(t, "fix_done", "QC", "system", `第 ${n} 轮修正完成`, { qc_rounds: n });
      } else return;
    }
  }

  function qcRecord(t, fail) {
    const rules = META.employee.quality_checks.map((q) => ({ rule: q.rule, name: q.name, passed: true, severity: ["length"].includes(q.rule) ? "warning" : "blocking", details: [] }));
    if (fail) {
      const r = rules.find((x) => x.rule === "data_consistency");
      r.passed = false;
      r.details = [{ msg: "正文“华东区环比 +12%”与核心指标表（-12.0%）不一致" }, { msg: "“华东区实收增长”与环比方向相反" }];
    }
    if (t.version > 1) rules.push({ rule: "feedback_addressed", name: "审核意见已采纳", passed: true, severity: "blocking", details: [] });
    return { round_no: t.qcRecords.length + 1, version: t.version, artifact: t.artifact && t.artifact.tag, artifact_sha256: t.artifact && t.artifact.sha256, ts: ts().replace("T", " ").slice(0, 19), passed: !fail, results: rules, fix_actions: fail ? [] : (t.artifact && t.artifact.fix_actions) || [] };
  }

  /* ------------------------------------------------------------ 周报内容（审核台预览） */
  function svgImg(kind) {
    const bars = kind === "region_target"
      ? `<rect x="40" y="40" width="210" height="22" fill="#4f8bff"/><rect x="40" y="80" width="188" height="22" fill="#4f8bff"/><rect x="40" y="120" width="127" height="22" fill="#4f8bff"/><rect x="40" y="160" width="84" height="22" fill="#4f8bff"/><line x1="290" y1="30" x2="290" y2="70" stroke="#d22d4f" stroke-width="2"/><text x="10" y="56" font-size="12">华东</text><text x="10" y="96" font-size="12">华南</text><text x="10" y="136" font-size="12">华北</text><text x="10" y="176" font-size="12">西南</text><text x="150" y="20" font-size="13" text-anchor="middle">区域实收与目标</text>`
      : `<polyline points="40,60 120,140 200,100 280,170" fill="none" stroke="#4f8bff" stroke-width="3"/><g fill="#4f8bff"><circle cx="40" cy="60" r="4"/><circle cx="120" cy="140" r="4"/><circle cx="200" cy="100" r="4"/><circle cx="280" cy="170" r="4"/></g><text x="160" y="20" font-size="13" text-anchor="middle">近 4 周实收趋势</text><text x="40" y="196" font-size="11" text-anchor="middle">W41</text><text x="280" y="196" font-size="11" text-anchor="middle">W44</text>`;
    return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 210" style="background:#fff;font-family:sans-serif">${bars}</svg>`);
  }
  function blocks(t) {
    const w = weekOf(t.form.period);
    const k = kpisFor(w);
    const v2 = t.version > 1;
    const B = (kind, value) => ({ kind, value });
    return [
      B("title", `经营周报 · 2026 年第 ${w} 周（10月26日—11月1日）`),
      B("meta", `统计周期：2026 年第 ${w} 周（10月26日—11月1日）　|　版本：V${t.version}　|　生成时间：`),
      B("h2", "一、本周摘要"),
      B("bullet", "本周全公司实收 573,635 元，环比下降 9.3%，毛利率 61.1%，外卖占比 31.8%，目标达成率 89.8%。"),
      B("bullet", "华东区实收环比下降 12.0%，主要受杭州湖滨店商场消防改造停业三天及上海静安店周边竞品开业影响。"),
      B("bullet", "华南区实收环比下降 12.7%，纪要提到“第二杯半价”活动已于 10 月 25 日结束，可能与此有关，具体影响待核实。"),
      B("bullet", "华北、西南区实收小幅下降，目标达成率均在 92% 以上，四个区域均未达成本周目标。"),
      B("h2", "二、核心指标"),
      B("p", "本周全公司实收 573,635 元，环比下降 9.3%；毛利率 61.1%，外卖占比 31.8%。四个区域实收均环比下降，其中华东、华南降幅超过 10%；目标达成率西南最高（96.3%），华东最低（84.1%）。" + (t.form.focus_region ? `重点关注的${t.form.focus_region}区本周实收与目标达成情况见下表。` : "")),
      B("table", { title: "核心指标", columns: ["范围", "本周实收", "环比", "毛利率", "外卖占比", "本周目标", "目标达成率"], rows: [["全公司", "573,635", "-9.3%", "61.1%", "31.8%", "639,090", "89.8%"]].concat(k.regions.map((r) => [r.name, Math.round(r.sales).toLocaleString("en-US"), (r.sales_wow * 100).toFixed(1) + "%", (r.gross_margin * 100).toFixed(1) + "%", (r.delivery_share * 100).toFixed(1) + "%", r.target.toLocaleString("en-US"), (r.target_rate * 100).toFixed(1) + "%"])) }),
      B("image_row", [{ caption: "region_target", url: svgImg("region_target") }, { caption: "weekly_trend", url: svgImg("weekly_trend") }]),
      B("h2", "三、品类与单品表现"),
      B("p", "饮品实收占比 31.0%，环比下降 14.2%，降幅最大；预包装食品小幅增长 2.1%。本周实收前 5 名单品依次为拿铁、生椰拿铁、美式咖啡、6寸生日蛋糕、草莓奶油切块。"),
      B("table", { title: "本周实收前 5 名单品", columns: ["排名", "单品", "品类", "本周实收", "环比"], rows: [["1", "拿铁", "饮品", "52,318", "-15.6%"], ["2", "生椰拿铁", "饮品", "41,906", "-18.2%"], ["3", "美式咖啡", "饮品", "33,215", "-11.0%"], ["4", "6寸生日蛋糕", "蛋糕", "30,840", "-4.3%"], ["5", "草莓奶油切块", "西点", "24,112", "-6.8%"]] }),
      B("h2", "四、异常与原因"),
      B("anomaly", { head: "A1 华东区：环比 -12.0%，变动 -26,898 元", phenomenon: "本周实收 197,618 元，上周 224,516 元；带动门店：杭州湖滨店（HZ01）环比 -38.3%，上海静安店（SH02）环比 -17.9%。", cause: (v2 ? "杭州湖滨店 10/27–10/29 因商场消防改造停业三天，属正常停业；上海静安店对面竞品 10/24 开业，分流明显，华东区经理已在周会上说明两家门店的影响。" : "杭州湖滨店 10/27–10/29 因商场消防改造停业三天；上海静安店对面竞品 10/24 开业。") + "（依据：第 44 周周会纪要 二、1；二、2）", suggestion: "杭州湖滨店复业后跟踪客流恢复；上海静安店评估会员日与新品试吃应对竞品。", checks: [] }),
      B("anomaly", { head: "A2 华南区：环比 -12.7%，变动 -25,814 元", phenomenon: "本周实收 177,306 元，上周 203,120 元；带动门店：深圳南山店（SZ01）环比 -15.3%，深圳福田店（SZ02）环比 -17.3%。", cause: "纪要提到“第二杯半价”活动已于 10 月 25 日结束，可能与此有关，具体影响待核实。（依据：第 44 周周会纪要 三、1）", suggestion: "复盘活动结束后的客单结构，评估是否以会员券替代全场促销。", checks: ["数据核对：纪要中“华南本周与上周基本持平”的说法与数据不一致，华南区实收环比 -12.7%，以数据为准。"] }),
      B("h2", "五、数据质量说明"),
      B("bullet", "本周退货 11 笔、合计 -260 元，已直接冲减实收。"),
      B("bullet", "杭州湖滨店 10/27–10/29 无销售数据：纪要说明为商场消防改造停业，属正常停业，不是数据缺失。"),
      B("bullet", "口径：实收 = 原价 − 折扣；按记录 ID 去重，本周未发现重复记录。"),
      B("h2", "六、下周关注事项"),
      B("bullet", "杭州湖滨店复业后客流恢复情况（负责人：华东区经理，11/06 前）。"),
      B("bullet", "上海静安店竞品应对方案（负责人：市场部，11/05 前）。"),
      B("bullet", "华南区活动结束后的销售走势，评估下一档促销。"),
      B("footer", "本报告由迷你数字员工生成，数字由程序按销售明细计算，经人工审核后交付。"),
    ];
  }

  /* ------------------------------------------------------------ 初始化：预置几条典型任务 */
  const ready = (async () => {
    clock = Date.now() - 3 * 3600 * 1000;
    const SAMPLE = { sales_file: ["销售明细.xlsx"], minutes_file: "周会纪要_2026W44.docx" };
    const a = newTask({ period: "2026-W44", focus_region: "华东" }, SAMPLE, [], clock);
    await drive(a, true);
    tickClock(120000);
    go(a, "request_changes", "REWORK", "reviewer", "华东区下滑原因没说清楚，请结合周会纪要补充", { version: 2, rework_rounds: 1, qc_rounds: 0 });
    ev(a, "review_submitted", null, { decision: "changes", comments: "华东区下滑原因没说清楚，请结合周会纪要补充", actor: "reviewer", version: 1 });
    a.feedback[2] = { comments: "华东区下滑原因没说清楚，请结合周会纪要补充", todos: [{ section: "anomalies", instruction: "华东区下滑原因没说清楚，请结合周会纪要补充", keywords: ["华东", "原因", "纪要"], metrics: {} }] };
    ev(a, "feedback_parsed", null, { version: 2, todos: a.feedback[2].todos });
    await drive(a, true);
    tickClock(90000);
    ev(a, "review_submitted", null, { decision: "approve", comments: "", actor: "reviewer", version: 2 });
    go(a, "approve", "DELIVERED", "reviewer", "审核通过");
    ev(a, "delivered", null, { dir: "deliverable", files: [{ name: "经营周报_2026W44.docx" }, { name: "qc_report.json" }, { name: "质检记录.md" }] });

    tickClock(600000);
    const b = newTask({ period: "2026-W43" }, { sales_file: ["销售明细.xlsx"], minutes_file: "周会纪要_2026W43.docx" }, [], clock);
    await drive(b, true);

    tickClock(500000);
    const c = newTask({ period: "2026-W44" }, SAMPLE, [{ tool: "search_minutes", mode: "timeout" }], clock);
    await drive(c, true);

    tickClock(400000);
    const d = newTask({ period: "2026-W46" }, { sales_file: ["销售明细.xlsx"] }, [], clock);
    await drive(d, true);
    clock = null;
  })();

  /* ------------------------------------------------------------ API */
  const brief = (t) => ({ id: t.id, state: t.state, label: LABELS[t.state], version: t.version, period: t.form.period || "", pause_reason: t.pause_reason, created_at: t.created_at, updated_at: t.updated_at, terminal: TERMINAL.includes(t.state), has_artifact: !!t.artifact, qc_rounds: t.qc_rounds, rework_rounds: t.rework_rounds });
  const find = (id) => { const t = tasks.find((x) => x.id === id); if (!t) throw { status: 404, error: `找不到：'${id}'` }; return t; };
  function detail(t) {
    const started = new Set(t.events.filter((e) => e.type === "step_started").map((e) => e.payload.key));
    const done = new Set(t.events.filter((e) => e.type === "step_completed" || e.type === "step_skipped").map((e) => e.payload.key));
    const w = weekOf(t.form.period);
    const hasK = t.events.some((e) => e.type === "step_completed" && e.payload.key === "kpi");
    return Object.assign(brief(t), {
      form: Object.fromEntries(Object.entries(t.form).filter(([k]) => !/_file$/.test(k))), file_names: t.file_names, faults: t.faults,
      steps: STEPS.map((s) => { const key = stepKey(t, s); return { id: s.id, name: s.name, type: s.type, status: done.has(key) ? "done" : started.has(key) ? "running" : "todo" }; }),
      artifact: t.artifact ? { ...t.artifact, primary_url: null } : null,
      deliverables: t.state === "DELIVERED" ? [{ name: `经营周报_2026W${w}.docx`, url: null, size: 182344 }, { name: "质检记录.md", url: null, size: 2210 }, { name: "qc_report.json", url: null, size: 6120 }, { name: "manifest.json", url: null, size: 640 }] : [],
      resume_to: t.resume_to, kpis: hasK ? kpisFor(w) : null,
    });
  }
  function review(t) {
    if (!t.artifact) throw { status: 404, error: "还没有可审核的产物" };
    const v2 = t.version > 1;
    return {
      task: brief(t), sha256: t.artifact.sha256, tag: t.artifact.tag, primary_url: null, blocks: blocks(t),
      qc: { rounds: t.qcRecords }, qc_markdown: "",
      diff: v2 ? "--- 上一版\n+++ 本版\n@@ -31 +31 @@\n-- 原因：杭州湖滨店 10/27–10/29 因商场消防改造停业三天；上海静安店对面竞品 10/24 开业。\n+- 原因：杭州湖滨店 10/27–10/29 因商场消防改造停业三天，属正常停业；上海静安店对面竞品 10/24 开业，分流明显，华东区经理已在周会上说明两家门店的影响。" : null,
      feedback: t.feedback[t.version] || null,
    };
  }
  const reject = (t, event, actor) => {
    const tr = TRANSITIONS.find((x) => x.src === t.state && x.event === event);
    let reason = null;
    if (!tr) reason = `非法流转：${t.state} 状态下不允许事件 ${event}`;
    else if (!tr.actors.includes(actor)) reason = `无权限：${event} 只能由 ${tr.actors.join("/")} 触发，当前为 ${actor}`;
    if (reason) { ev(t, "transition_rejected", null, { from: t.state, event, actor, reason }); throw { status: 409, error: `操作被拒绝：${reason}` }; }
  };

  async function request(method, path, body) {
    await ready;
    body = body || {};
    let m;
    if (method === "GET") {
      if (path === "/api/meta") return META;
      if (path === "/api/tasks") return tasks.map(brief);
      if ((m = path.match(/^\/api\/tasks\/([\w-]+)$/))) return detail(find(m[1]));
      if ((m = path.match(/^\/api\/tasks\/([\w-]+)\/events$/))) return find(m[1]).events;
      if ((m = path.match(/^\/api\/tasks\/([\w-]+)\/review$/))) return review(find(m[1]));
      if (path === "/api/faults") return globalFaults;
      if (path === "/api/config") return { "employee.yaml": CONFIG_YAML, "SKILL.md": CONFIG_SKILL };
    } else {
      if (path === "/api/tasks") {
        const files = {};
        Object.entries(body.files || {}).forEach(([k, v]) => { if (v && v.length) files[k] = k === "sales_file" ? v.map((x) => x.name) : v[v.length - 1].name; });
        if (body.use_sample) { files.sales_file = files.sales_file || ["销售明细.xlsx"]; files.minutes_file = files.minutes_file || "周会纪要_2026W44.docx"; }
        const faults = (body.faults || []).map((id) => FAULTS.find((f) => f.id === id)).filter(Boolean).map(({ tool, mode }) => ({ tool, mode }));
        const t = newTask(Object.fromEntries(Object.entries(body.values || {}).filter(([, v]) => v)), files, faults);
        setTimeout(() => drive(t, false), 50);
        return { id: t.id };
      }
      if ((m = path.match(/^\/api\/tasks\/([\w-]+)\/(supplement|retry|recheck|cancel|review)$/))) {
        const t = find(m[1]);
        const role = body.role || "initiator";
        if (m[2] === "cancel") { reject(t, "cancel", role); go(t, "cancel", "CANCELLED", role, "发起人取消"); }
        else if (m[2] === "supplement") {
          reject(t, "supplement", role);
          Object.entries(body.files || {}).forEach(([k, v]) => { if (v && v.length) t.file_names[k] = k === "sales_file" ? (t.file_names[k] || []).concat(v.map((x) => x.name)) : v[v.length - 1].name; });
          go(t, "supplement", "ADMISSION", role, "补充：" + Object.keys(body.files || {}).join(", "));
          setTimeout(() => drive(t, false), 50);
        } else if (m[2] === "retry") {
          reject(t, "retry", role);
          if (body.clear_faults !== false) t.faults = [];
          const to = t.resume_to || "RUNNING";
          go(t, "retry", to, role, "人工重试", { resume_to: null });
          ev(t, "task_resumed", null, { state: to });
          setTimeout(() => drive(t, false), 50);
        } else if (m[2] === "recheck") {
          reject(t, "manual_recheck", role);
          if (!t.artifact) throw { status: 409, error: "操作被拒绝：还没有可质检的产物，请使用重试" };
          go(t, "manual_recheck", "QC", role, "人工处理后重新质检", { qc_rounds: 0 });
          setTimeout(() => drive(t, false), 50);
        } else if (m[2] === "review") {
          ev(t, "review_submitted", null, { decision: body.decision, comments: body.comments || "", actor: role, version: t.version, reviewed_sha256: body.sha256 });
          if (body.decision === "approve") {
            reject(t, "approve", role);
            if (body.sha256 !== t.artifact.sha256) { const r = "闸门未通过：审核的版本与当前产物不一致（文件哈希不同），请刷新后重新审核"; ev(t, "transition_rejected", null, { from: t.state, event: "approve", actor: role, reason: r }); throw { status: 409, error: "操作被拒绝：" + r }; }
            go(t, "approve", "DELIVERED", role, "审核通过");
            ev(t, "delivered", null, { dir: "deliverable", files: [{ name: "经营周报.docx" }] });
          } else {
            reject(t, "request_changes", role);
            if (!(body.comments || "").trim()) { const r = "闸门未通过：退回修改必须填写意见"; ev(t, "transition_rejected", null, { from: t.state, event: "request_changes", actor: role, reason: r }); throw { status: 409, error: "操作被拒绝：" + r }; }
            if (t.rework_rounds >= 3) { const r = "已返工 3 次仍未通过，请审核人与发起人线下沟通后处理"; go(t, "request_changes_exhausted", "SUSPENDED", role, r, { pause_reason: r, resume_to: "PENDING_REVIEW" }); return brief(t); }
            t.feedback[t.version + 1] = { comments: body.comments, todos: String(body.comments).split(/[；;\n]/).filter((x) => x.trim()).map((x) => ({ section: /原因|下滑|异常|门店/.test(x) ? "anomalies" : /摘要|结论/.test(x) ? "summary" : "next_week", instruction: x.trim(), keywords: (x.match(/华东|华南|华北|西南|原因|纪要|摘要|建议/g) || []).slice(0, 4), metrics: {} })) };
            go(t, "request_changes", "REWORK", role, body.comments, { version: t.version + 1, rework_rounds: t.rework_rounds + 1, qc_rounds: 0 });
            ev(t, "feedback_parsed", null, { version: t.version, todos: t.feedback[t.version].todos });
            setTimeout(() => drive(t, false), 50);
          }
        }
        return brief(t);
      }
      if (path === "/api/faults") { globalFaults = (body.ids || []).map((id) => FAULTS.find((f) => f.id === id)).filter(Boolean).map(({ tool, mode }) => ({ tool, mode })); return globalFaults; }
      if (path === "/api/config") {
        const text = String(body.text || "");
        const errors = [];
        if (body.name === "employee.yaml" && /^\s*tools:\s*\[.*\bnot_a_tool\b/m.test(text)) errors.push("tools 中的 not_a_tool 不是已注册的工具");
        if (body.name === "employee.yaml" && /\t/.test(text)) errors.push("YAML 不能用 Tab 缩进");
        return { saved: !errors.length && body.save !== false, errors, warnings: [] };
      }
    }
    throw { status: 404, error: "接口不存在" };
  }

  const CONFIG_YAML = `# 经营周报员工（场景 A：青禾烘焙）—— 结构配置。做法（岗位职责、写作要求、注意事项）在 SKILL.md。
# 演示数据：连接后端后显示真实的 employee.yaml
id: weekly_report
name: 经营周报员工
skill: SKILL.md

form:
  - {key: period, label: 统计周期, type: iso_week, required: true, help: "如 2026-W44 或 2026年第44周"}
  - {key: sales_file, label: 销售明细, type: file, ext: [xlsx, csv], required: true, multiple: true}
  - {key: minutes_file, label: 周会纪要, type: file, ext: [docx, md], required: true}

analysis:
  anomaly_threshold: 0.10       # 区域或门店实收环比超过 ±10% 必须逐一列出并说明原因
  top_items: 5

quality_checks:
  - rule: structure
    params: {min_charts: 2, required_tables: [核心指标, 品类销售, 本周实收前 5 名单品]}
  - rule: data_consistency
  - rule: cause_evidence
    params: {pending_word: 待核实}

limits:
  max_model_turns_per_step: 8
  max_step_seconds: 180
  qc_max_fix_rounds: 2
  rework_max_rounds: 3
`;
  const CONFIG_SKILL = `---
name: weekly-report
description: 经营周报员工。根据门店销售明细和周会纪要，为连锁烘焙品牌写一份管理层打开第一页就能看到结论的经营周报。
---

# 经营周报员工

## 岗位职责
你是连锁烘焙品牌运营部的经营分析员。每周一根据上周门店销售明细和周会纪要撰写经营周报。
（演示数据：连接后端后显示真实的 SKILL.md）

## 异常分析
原因只能来自纪要原文，纪要中找不到依据的，写“周会纪要未提及相关情况，原因待核实”。

## 注意事项
- 所有数字必须用占位符引用，例如 \`{{total.sales}}\`，不要自己写数字或做计算。
- 数据里没有的指标（客单价、同比、客流等）一律不写。
`;

  window.MockAPI = { request };
})();
