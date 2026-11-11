/* 迷你数字员工 · 控制台：路由、页面与接口调用。界面只展示和发起操作，所有判定都在后端程序里。 */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const AUTO = ["CREATED", "ADMISSION", "RUNNING", "QC", "AUTO_FIXING", "REWORK"];
  const TERMINAL = ["DELIVERED", "REJECTED", "CANCELLED"];
  const MOOD_OF = { CREATED: "running", ADMISSION: "running", RUNNING: "running", QC: "running", AUTO_FIXING: "running", REWORK: "running", WAITING_INPUT: "waiting", SUSPENDED: "danger", PENDING_REVIEW: "review", DELIVERED: "success", REJECTED: "idle", CANCELLED: "idle" };
  const ORB_OF = Object.assign({}, MOOD_OF, { REJECTED: "muted", CANCELLED: "muted" });
  const EDITABLE = ["SUSPENDED", "PENDING_REVIEW"]; // 有报告时可人工在线修改，修改后重新质检
  const PROGRESS = { CREATED: 0.05, ADMISSION: 0.14, WAITING_INPUT: 0.2, RUNNING: 0.45, SUSPENDED: 0.5, QC: 0.72, AUTO_FIXING: 0.78, REWORK: 0.6, PENDING_REVIEW: 0.9, DELIVERED: 1, REJECTED: 1, CANCELLED: 1 };
  const ACTOR = { system: "系统", initiator: "发起人", reviewer: "审核人" };
  const I = {
    arrow: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 5l7 7-7 7"/></svg>',
    upload: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4M6 10l6-6 6 6M4 20h16"/></svg>',
    download: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v12M6 10l6 6 6-6M4 20h16"/></svg>',
    play: '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M7 4v16l13-8z"/></svg>',
    pause: '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M6 4h4v16H6zM14 4h4v16h-4z"/></svg>',
    restart: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5"/></svg>',
    close: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    clip: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11l-8.5 8.5a5 5 0 0 1-7-7L14 4a3.5 3.5 0 0 1 5 5l-8.5 8.5a2 2 0 0 1-3-3L15 7"/></svg>',
    boltSm: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M13 2L4 14h7l-1 8 9-12h-7z"/></svg>',
    bolt: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M13 2L4 14h7l-1 8 9-12h-7z"/></svg>',
  };
  const roll = (t) => `<span class="roll"><span>${t}</span><span>${t}</span></span>`;
  const time = (ts) => (ts ? String(ts).slice(11, 19) : "");
  const short = (ts) => (ts ? String(ts).slice(5, 16).replace("T", " ") : "");
  const kb = (n) => (n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");
  const stateChip = (s, label, big) => `<span class="state ${big ? "state-lg" : ""} ${AUTO.includes(s) ? "live" : ""}" data-s="${esc(s)}">${esc(label || s)}</span>`;

  const S = { meta: null, mock: false, tasks: [], faults: [], role: "initiator" };
  try { S.role = localStorage.getItem("me-role") || "initiator"; } catch (e) { /* 隐私模式 */ }
  // 姓名用于留痕和“发起人不能审核自己发起的任务”；管理员口令只留在本页内存里，保存配置、开关全局故障时随请求发送
  S.users = { initiator: "张三", reviewer: "李四" };
  try { for (const r in S.users) S.users[r] = localStorage.getItem("me-user-" + r) || S.users[r]; } catch (e) { /* ignore */ }
  S.adminPw = "";
  const L = (s) => (S.meta && S.meta.states[s]) || s || "断点";

  /* ------------------------------------------------------------ 接口 */
  class ApiErr extends Error { constructor(m, status, data) { super(m); this.status = status; this.data = data || null; } }
  const API = {
    async call(method, path, body) {
      if (S.mock) {
        try { return JSON.parse(JSON.stringify(await MockAPI.request(method, path, body))); }
        catch (e) { throw new ApiErr(e.error || String(e.message || e), e.status || 500); }
      }
      let res;
      try {
        res = await fetch(path, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
      } catch (e) { throw new ApiErr("无法连接后端服务，请确认 web_server.py 正在运行", 0); }
      let data = null;
      try { data = await res.json(); } catch (e) { /* 非 JSON */ }
      if (!res.ok) throw new ApiErr((data && (data.error + (data.details ? "：" + data.details.join("；") : ""))) || `HTTP ${res.status}`, res.status, data);
      return data;
    },
    get: (p) => API.call("GET", p),
    post: (p, b) => API.call("POST", p, { role: S.role, ...(b || {}), user: S.users[S.role],
      ...(p === "/api/config" || p === "/api/faults" ? { admin_password: S.adminPw } : {}) }),
  };

  /* ------------------------------------------------------------ 页面作用域（离开页面时清理定时器） */
  function makeScope(root) {
    const s = {
      root, alive: true, timers: [], cleanups: [],
      every(fn, ms) { const id = setInterval(() => s.alive && fn(), ms); s.timers.push(id); return id; },
      later(fn, ms) { const id = setTimeout(() => s.alive && fn(), ms); s.timers.push(id); return id; },
      poll(fn, msFn) {
        let busy = false;
        const loop = async () => {
          if (!s.alive) return;
          if (!busy) { busy = true; try { await fn(); } catch (e) { console.warn(e); } busy = false; }
          s.later(loop, typeof msFn === "function" ? msFn() : msFn);
        };
        s.later(loop, typeof msFn === "function" ? msFn() : msFn);
      },
      on(fn) { s.cleanups.push(fn); },
      listen(target, ev, fn) { target.addEventListener(ev, fn); s.cleanups.push(() => target.removeEventListener(ev, fn)); },
      dispose() { s.alive = false; s.timers.forEach((id) => { clearInterval(id); clearTimeout(id); }); s.cleanups.forEach((f) => { try { f(); } catch (e) { /* ignore */ } }); },
    };
    return s;
  }

  /* ------------------------------------------------------------ 路由 */
  const NAV = [["", "总览", "01"], ["new", "发起任务", "02"], ["tasks", "任务中心", "03"], ["review", "审核台", "04"], ["events", "事件回放", "05"], ["faults", "故障注入", "06"], ["config", "员工配置", "07"]];
  const ROUTES = [
    [/^$/, "home"], [/^new$/, "newTask"], [/^tasks$/, "tasks"], [/^tasks\/([\w-]+)$/, "task"],
    [/^review$/, "reviewList"], [/^review\/([\w-]+)$/, "review"], [/^events$/, "events"], [/^events\/([\w-]+)$/, "events"],
    [/^faults$/, "faults"], [/^config$/, "config"],
  ];
  const NAV_OF = { home: "", newTask: "new", tasks: "tasks", task: "tasks", reviewList: "review", review: "review", events: "events", faults: "faults", config: "config" };
  const TITLE = { home: "总览", newTask: "发起任务", tasks: "任务中心", task: "任务详情", reviewList: "审核台", review: "审核台", events: "事件回放", faults: "故障注入", config: "员工配置" };
  let current = null, first = true, navSeq = 0;

  async function curtain(label) {
    const c = $("#curtain");
    c.innerHTML = [0, 1, 2, 3, 4].map((i) => `<i style="--i:${i}"></i>`).join("") + `<div class="curtain-label">${esc(label)}</div>`;
    c.className = "";
    void c.offsetWidth;
    c.className = "in";
    await wait(560);
    return () => {
      c.className = "out";
      setTimeout(() => { if (c.className === "out") { c.className = ""; c.innerHTML = ""; } }, 900);
    };
  }

  async function navigate() {
    const my = ++navSeq;
    const hash = location.hash.replace(/^#\/?/, "");
    let name = "home", params = [];
    for (const [re, n] of ROUTES) { const m = hash.match(re); if (m) { name = n; params = m.slice(1); break; } }
    const same = current && current.name === name;
    setActiveNav(NAV_OF[name]);
    document.body.classList.remove("menu-open");
    let finish = null;
    if (!first && !same && !FX.reduce) finish = await curtain(TITLE[name]);
    if (my !== navSeq) return;
    if (current) current.scope.dispose();
    FX.clearFx();
    const view = $("#view");
    view.innerHTML = "";
    if (!same) scrollTo(0, 0);
    const scope = makeScope(view);
    current = { name, scope };
    FX.mood(name === "review" || name === "reviewList" ? "review" : "idle");
    try { await PAGES[name](scope, ...params); }
    catch (e) {
      console.error(e);
      view.innerHTML = `<div class="empty"><div class="big">出了点问题</div><p>${esc(e.message || e)}</p><a class="btn btn-ghost" href="#/">${roll("回到总览")}</a></div>`;
    }
    if (scope.alive) FX.bind(view);
    if (finish) finish();
    first = false;
    document.title = `${TITLE[name]} · 迷你数字员工`;
  }

  function setActiveNav(key) {
    $$(".nav a, .overlay-menu a[data-k]").forEach((a) => a.classList.toggle("active", a.dataset.k === key));
  }

  /* ------------------------------------------------------------ 顶栏、身份、全局轮询 */
  function buildChrome() {
    $(".nav").innerHTML = NAV.map(([k, t, n]) => `<a href="#/${k}" data-k="${k}"><span class="idx">${n}</span>${t}<span class="badge-slot"></span></a>`).join("");
    $(".overlay-menu").innerHTML = NAV.map(([k, t, n]) => `<a href="#/${k}" data-k="${k}"><span class="idx">${n}</span><span class="t">${t}</span></a>`).join("") +
      `<div class="om-role"></div>`;
    $$(".overlay-menu a span.t").forEach((s, i) => (s.style.transitionDelay = `${0.25 + i * 0.05}s`));
    $(".model-pill").innerHTML = `<i class="live-dot"></i>${S.mock ? "演示数据" : "模型 · " + esc(S.meta.model)}`;
    renderRole();
    $(".menu-btn").addEventListener("click", () => document.body.classList.toggle("menu-open"));
    const brand = $(".brand canvas");
    if (brand) FX.orb(brand, { n: 150, rings: false, radius: 0.42, follow: false, dot: 1.2, speed: 0.01 });
    let lastY = 0;
    addEventListener("scroll", () => {
      const y = scrollY;
      const max = Math.max(1, document.documentElement.scrollHeight - innerHeight);
      $(".scroll-progress").style.transform = `scaleX(${Math.min(1, y / max)})`;
      $(".topbar").classList.toggle("hide", y > 160 && y > lastY && !document.body.classList.contains("menu-open"));
      lastY = y;
    }, { passive: true });
  }
  function renderRole() {
    const html = `<div class="role-switch" data-role="${S.role}"><i class="thumb"></i>
      <button data-r="initiator" class="${S.role === "initiator" ? "on" : ""}">发起人</button><button data-r="reviewer" class="${S.role === "reviewer" ? "on" : ""}">审核人</button></div>
      <input class="role-name" value="${esc(S.users[S.role])}" maxlength="20" spellcheck="false" aria-label="姓名" title="姓名：记录在事件日志里；发起人不能审核自己发起的任务">`;
    $$(".role-host, .om-role").forEach((h) => {
      h.innerHTML = html;
      h.querySelector(".role-name").addEventListener("change", (e) => {
        S.users[S.role] = e.target.value.trim() || S.users[S.role];
        try { localStorage.setItem("me-user-" + S.role, S.users[S.role]); } catch (err) { /* ignore */ }
        renderRole();
      });
      const sw = h.querySelector(".role-switch");
      const place = () => {
        const on = sw.querySelector("button.on");
        const th = sw.querySelector(".thumb");
        if (on && th) { th.style.width = on.offsetWidth + "px"; th.style.transform = `translateX(${on.offsetLeft - 4}px)`; }
      };
      requestAnimationFrame(place);
      setTimeout(place, 300);
      sw.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
        if (S.role === b.dataset.r) return;
        S.role = b.dataset.r;
        try { localStorage.setItem("me-role", S.role); } catch (e) { /* ignore */ }
        renderRole();
        FX.toast(`已切换为${ACTOR[S.role]}`, "info", 2200);
        dispatchEvent(new CustomEvent("rolechange"));
      }));
    });
  }
  async function pollGlobal() {
    try {
      S.tasks = await API.get("/api/tasks");
      const attn = S.tasks.filter((t) => t.state === "WAITING_INPUT" || t.state === "SUSPENDED").length;
      const rev = S.tasks.filter((t) => t.state === "PENDING_REVIEW").length;
      setBadge("tasks", attn);
      setBadge("review", rev);
    } catch (e) { /* 后端暂时不可用时保持现状 */ }
    try {
      S.faults = await API.get("/api/faults");
      renderHazard();
    } catch (e) { /* ignore */ }
  }
  function setBadge(k, n) {
    const a = $(`.nav a[data-k="${k}"] .badge-slot`);
    if (!a) return;
    const cur = a.firstChild ? +a.firstChild.textContent : 0;
    if (cur === n) return;
    a.innerHTML = n ? `<span class="badge-n">${n}</span>` : "";
  }
  function renderHazard() {
    const hz = $("#hazard");
    const on = S.faults && S.faults.length;
    const names = (S.faults || []).map((f) => ((S.meta.faults || []).find((x) => x.tool === f.tool && x.mode === f.mode) || {}).label || `${f.tool}:${f.mode}`);
    hz.hidden = !on;
    document.body.classList.toggle("has-hazard", !!on);
    if (on) hz.innerHTML = `<span class="hz-text">⚠ 全局故障注入中：${esc(names.join(" · "))} —— 演示结束记得关闭</span>`;
  }

  /* ------------------------------------------------------------ 公共组件 */
  function isoWeek(text) {
    const s = String(text || "").trim().toUpperCase();
    // 与后端 periods.py 一致：完整一周的日期范围（周一至周日）
    const D = "(\\d{4})[-/.年](\\d{1,2})[-/.月](\\d{1,2})日?";
    const r = s.match(new RegExp(`^${D}\\s*(?:~|～|至|到|—|–|-{1,2})\\s*${D}$`));
    let m = s.match(/^(\d{4})\s*-?\s*W\s*(\d{1,2})$/) || s.match(/^(\d{4})\s*年\s*第?\s*(\d{1,2})\s*周$/);
    if (r) {
      const a = new Date(Date.UTC(+r[1], +r[2] - 1, +r[3])), b = new Date(Date.UTC(+r[4], +r[5] - 1, +r[6]));
      if (a.getUTCMonth() !== +r[2] - 1 || b.getUTCMonth() !== +r[5] - 1) return { bad: "日期不存在" };
      if (a.getUTCDay() !== 1 || (b - a) / 86400000 !== 6) return { bad: "须为完整的 ISO 周（周一至周日）" };
      const th = new Date(a); th.setUTCDate(a.getUTCDate() + 3);
      const y0 = th.getUTCFullYear(), wk = Math.ceil(((th - Date.UTC(y0, 0, 1)) / 86400000 + 1) / 7);
      m = [s, String(y0), String(wk)];
    }
    if (!m) return null;
    const y = +m[1], w = +m[2];
    const jan4 = new Date(Date.UTC(y, 0, 4));
    const dow = jan4.getUTCDay() || 7;
    const mon = new Date(jan4);
    mon.setUTCDate(jan4.getUTCDate() - dow + 1 + (w - 1) * 7);
    const sun = new Date(mon);
    sun.setUTCDate(mon.getUTCDate() + 6);
    const wk = new Date(Date.UTC(y, 11, 28));
    const maxW = (() => { const d = new Date(wk); const day = d.getUTCDay() || 7; d.setUTCDate(d.getUTCDate() + 4 - day); const ys = new Date(Date.UTC(d.getUTCFullYear(), 0, 1)); return Math.ceil(((d - ys) / 86400000 + 1) / 7); })();
    if (w < 1 || w > maxW) return { y, w, bad: `${y} 年没有第 ${w} 周` };
    const md = (d) => `${d.getUTCMonth() + 1}月${d.getUTCDate()}日`;
    return { y, w, key: `${y}-W${String(w).padStart(2, "0")}`, label: `${y} 年第 ${w} 周（${md(mon)}—${md(sun)}）` };
  }

  const readB64 = (file) => new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result).split(",")[1] || "");
    r.onerror = () => rej(new Error("读取文件失败：" + file.name));
    r.readAsDataURL(file);
  });
  async function packFiles(store) {
    const out = {};
    for (const [k, list] of Object.entries(store)) {
      if (!list.length) continue;
      out[k] = [];
      for (const f of list) out[k].push({ name: f.name, data: await readB64(f) });
    }
    return out;
  }
  function dropZone(field, store, onChange) {
    const ext = (field.ext || []).map((e) => "." + e).join(",");
    const wrap = document.createElement("div");
    wrap.className = "field";
    wrap.innerHTML = `<div class="field-label"><b>${esc(field.label)}${field.required ? '<span class="req">*</span>' : ""}</b><span class="mono faint">${esc((field.ext || []).join(" / ").toUpperCase())}${field.multiple ? " · 可多份" : ""}</span></div>
      <label class="drop" data-cursor="选择文件"><input type="file" accept="${esc(ext)}" ${field.multiple ? "multiple" : ""}>
        <div class="ico">${I.upload}</div><div><div class="t1">拖入或点击选择${esc(field.label)}</div><div class="t2">${esc(field.help || "支持 " + (field.ext || []).join("、"))}</div></div></label>
      <div class="files"></div>`;
    store[field.key] = store[field.key] || [];
    const drop = wrap.querySelector(".drop"), input = wrap.querySelector("input"), files = wrap.querySelector(".files");
    const render = () => {
      files.innerHTML = store[field.key].map((f, i) => `<span class="file-chip">${esc(f.name)} <span class="faint">${kb(f.size)}</span><button type="button" data-i="${i}" aria-label="移除">${I.close}</button></span>`).join("");
      drop.classList.toggle("has", store[field.key].length > 0);
      files.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { store[field.key].splice(+b.dataset.i, 1); render(); onChange && onChange(); }));
    };
    const add = (list) => {
      const ok = [], bad = [];
      [...list].forEach((f) => ((field.ext || []).some((e) => f.name.toLowerCase().endsWith("." + e)) ? ok : bad).push(f));
      if (bad.length) FX.toast(`${field.label}只支持 ${(field.ext || []).join("、")}：${bad.map((f) => f.name).join("、")}`, "warn");
      if (!ok.length) return;
      store[field.key] = field.multiple ? store[field.key].concat(ok) : [ok[ok.length - 1]];
      render();
      onChange && onChange();
    };
    input.addEventListener("change", () => { add(input.files); input.value = ""; });
    ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => add(e.dataTransfer.files));
    render();
    return wrap;
  }

  // 必填但发起人可确认“没有这份材料”（配置 waivable）：勾选后不上传也可继续，程序记录确认人
  function waiveToggle(f, values, onChange) {
    const el = document.createElement("label");
    el.className = "toggle";
    el.style.margin = "-6px 0 22px";
    el.innerHTML = `<input type="checkbox"><span class="sw"></span><span>${esc(f.waivable)}</span>`;
    el.querySelector("input").addEventListener("change", (e) => {
      if (e.target.checked) values["waive_" + f.key] = "1"; else delete values["waive_" + f.key];
      if (onChange) onChange();
    });
    return el;
  }
  // 保存配置、开关全局故障需要管理员口令（.env 中的 MINI_ADMIN_PASSWORD），由后端校验
  function adminBar(what) {
    return `<div class="card admin-bar" style="margin-bottom:18px;display:flex;gap:14px;align-items:center;flex-wrap:wrap">
      <span class="card-title" style="margin:0"><span class="dot" style="background:var(--amber)"></span>管理员</span>
      ${S.meta.admin_configured === false ? `<span class="muted" style="font-size:13px">未设置管理员口令：在 .env 中添加 MINI_ADMIN_PASSWORD 后重启服务，否则${esc(what)}不可用。</span>` :
      `<div class="input-line" style="flex:1;min-width:200px;max-width:320px"><input type="password" class="admin-pw" placeholder="管理员口令" autocomplete="off" value="${esc(S.adminPw)}"></div>
      <span class="faint" style="font-size:12px">${esc(what)}需要管理员口令；其他人可查看。</span>`}</div>`;
  }
  function bindAdminBar(root) {
    const pw = root.querySelector(".admin-pw");
    if (pw) pw.addEventListener("input", () => { S.adminPw = pw.value; });
  }
  // 退回前程序用数据核对意见本身：可能有误时列出问题，审核人改意见，或确认后仍提交
  function feedbackCheckBox(host, issues, onConfirm) {
    host.innerHTML = `<div class="alert warn" style="margin-top:14px"><span class="a-ico">!</span><h4>审核意见可能有误，请确认</h4>
      <p>${issues.map((i) => `“${esc(i.instruction)}”：${esc(i.msg)}`).join("<br>")}</p>
      <p class="faint" style="font-size:13px">可修改意见后重新提交；确认无误也可仍然提交，员工会按意见处理，确实无法落实的会说明理由，由程序核实。</p>
      <div class="btns" style="margin-top:10px"><button class="btn btn-sm btn-ghost" id="fcGo">${roll("确认无误，仍然提交")}</button></div></div>`;
    $("#fcGo", host).addEventListener("click", (e) => onConfirm(e.currentTarget));
  }
  function busy(btn, on) { btn.classList.toggle("loading", on); btn.disabled = on; }
  function centerOf(el) { const r = el.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }
  async function act(btn, fn, okMsg) {
    if (btn) busy(btn, true);
    try {
      const r = await fn();
      if (okMsg) FX.toast(okMsg, "ok");
      return r || true;
    } catch (e) {
      FX.toast(e.message || String(e), "err", 6500);
      if (btn) { const [x, y] = centerOf(btn); FX.shock(x, y); }
      return null;
    } finally { if (btn) busy(btn, false); }
  }

  function openDrawer(title, obj) {
    const d = $("#drawer");
    d.innerHTML = `<div class="scrim"></div><div class="panel"><div class="ph"><b>${esc(title)}</b><button class="icon-btn" aria-label="关闭">${I.close}</button></div><div class="pb"><pre class="json">${jsonHL(obj)}</pre></div></div>`;
    requestAnimationFrame(() => d.classList.add("on"));
    const close = () => { d.classList.remove("on"); removeEventListener("keydown", onKey); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    d.querySelector(".scrim").addEventListener("click", close);
    d.querySelector(".icon-btn").addEventListener("click", close);
    addEventListener("keydown", onKey);
  }
  function jsonHL(obj) {
    const s = JSON.stringify(obj, null, 2).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    return s.replace(/("(\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false)\b|\bnull\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g, (m) => {
      let c = "n";
      if (/^"/.test(m)) c = /:$/.test(m) ? "k" : "s";
      else if (/true|false/.test(m)) c = "b";
      else if (/null/.test(m)) c = "z";
      return `<span class="${c}">${m}</span>`;
    });
  }

  /* 事件摘要（与 Streamlit 版回放一致） */
  const EV_COLOR = { state_changed: "var(--lime)", transition_rejected: "var(--rose)", tool_called: "var(--aqua)", tool_failed: "var(--rose)", tool_denied: "var(--rose)", model_called: "var(--violet)", model_output_invalid: "var(--amber)", qc_run: "var(--amber)", review_submitted: "var(--blue)", feedback_parsed: "var(--blue)", delivered: "var(--lime)", error: "var(--rose)", task_resumed: "var(--amber)" };
  function evBrief(e) {
    const p = e.payload || {};
    switch (e.type) {
      case "state_changed": return `${L(p.from)} → ${L(p.to)}（${p.event}，${ACTOR[p.actor] || p.actor}）${p.reason ? " " + p.reason : ""}`;
      case "tool_called": return `${p.tool} 第${p.attempt}次 · ${p.ms}ms`;
      case "tool_failed": return `${p.tool} 第${p.attempt ?? "?"}次失败：${p.error}`;
      case "tool_denied": return `拒绝调用 ${p.tool}（${p.allowed ? "本步开放：" + p.allowed.join("、") : p.reason || ""}）`;
      case "model_called": return `第${p.turn}轮 · ${p.ms}ms · 工具调用：${(p.tool_calls || []).map((c) => c.name).join("、") || "无"}${p.usage && p.usage.total_tokens ? " · " + p.usage.total_tokens + " tokens" : ""}`;
      case "model_output_invalid": return `第${p.turn}轮输出不合格：${p.error}`;
      case "qc_run": return `第${p.round_no}次质检 ${p.passed ? "全部通过" : "未通过：" + (p.failed || []).join("、")}`;
      case "transition_rejected": return p.reason;
      case "step_started": return `开始「${p.name || p.key}」`;
      case "step_completed": return `完成 ${p.key}${p.ms ? " · " + p.ms + "ms" : ""}`;
      case "step_skipped": return `跳过 ${p.key}（${p.reason || "已完成"}）`;
      case "review_submitted": return `${p.decision === "approve" ? "审核通过" : "退回修改"}${p.comments ? "：" + p.comments : ""}（${ACTOR[p.actor] || p.actor}）`;
      case "feedback_checked": return `退回前检查：意见可能有误（${(p.issues || []).map((i) => i.msg).join("；")}），已提示审核人确认`;
      case "feedback_parsed": return `审核意见拆成 ${(p.todos || []).length} 条待办（V${p.version}）` + (p.todos || []).map((t) => (t.targets ? `；“${t.instruction}”对应异常 ${t.targets.map((x) => x.label).join("、")}` : "") + (t.repeat ? `，该意见已是第 ${t.repeat} 次提出` : "")).join("");
      case "delivered": return `生成交付包 · ${(p.files || []).length} 个文件`;
      case "task_created": return `发起任务 · 统计周期 ${(p.form && p.form.period) || "—"}`;
      case "form_changed": return `修改表单：${Object.entries(p.fields || {}).map(([k, c]) => `${k} ${c.from || "—"} → ${c.to}`).join("；")}${p.by ? "（" + p.by + "）" : ""}`;
      case "material_waived": return `发起人确认没有：${(p.fields || []).join("、")}${p.by ? "（" + p.by + "）" : ""}，继续执行`;
      case "task_resumed": return `断点恢复，从「${L(p.state)}」继续${p.reason ? "：" + p.reason : ""}`;
      case "admission_checked": return `准入检查${p.problems && p.problems.length ? "：" + p.problems.join("；") : "完成"}`;
      case "form_validated": return "表单校验通过";
      case "error": return `${p.error}`;
      default: return JSON.stringify(p).slice(0, 140);
    }
  }
  function feedRow(e) {
    const c = e.type === "qc_run" && e.payload && e.payload.passed ? "var(--lime)" : EV_COLOR[e.type] || "var(--ink-2)";
    const row = document.createElement("div");
    row.className = "feed-row";
    row.innerHTML = `<span class="seq">#${e.seq}</span><span class="ts">${esc(time(e.ts))}${esc(String(e.ts || "").slice(19, 23))}</span><span class="ty" style="--tc:${c}">${esc(e.type)}</span><span class="msg" title="${esc(evBrief(e))}">${e.step ? `<span class="faint">[${esc(e.step)}]</span> ` : ""}${esc(evBrief(e))}</span>`;
    row.addEventListener("click", () => openDrawer(`#${e.seq} ${e.type}`, e));
    return row;
  }

  function pageHead(idx, title, sub, right = "") {
    return `<div class="page-head"><div><div class="eyebrow" data-reveal><b>${idx}</b> / ${esc(TITLE_EN[idx] || "")}</div>
      <h1 class="page-title" data-split>${title}</h1>${sub ? `<p class="page-sub" data-reveal style="--d:250ms">${sub}</p>` : ""}</div>${right}</div>`;
  }
  const TITLE_EN = { "01": "Overview", "02": "New task", "03": "Task center", "04": "Review desk", "05": "Event replay", "06": "Fault injection", "07": "Employee config" };

  /* ================================================================ 01 总览 */
  async function pageHome(scope) {
    const meta = S.meta, emp = meta.employee || { steps: [], tools: [], quality_checks: [], limits: {} };
    const tasks = (S.tasks = await API.get("/api/tasks"));
    const active = tasks.find((t) => AUTO.includes(t.state));
    const cnt = {
      total: tasks.length, delivered: tasks.filter((t) => t.state === "DELIVERED").length,
      review: tasks.filter((t) => t.state === "PENDING_REVIEW").length,
      attn: tasks.filter((t) => t.state === "WAITING_INPUT" || t.state === "SUSPENDED").length,
    };
    const steps = emp.steps || [];
    scope.root.innerHTML = `
    <section class="hero">
      <div>
        <div class="eyebrow" data-reveal>锐软科技赛题 · 场景 A · <b>${esc(emp.name || "经营周报员工")}</b></div>
        <h1 class="hero-title"><span class="line" data-split>迷你</span><span class="line gradient-text" data-split data-delay="140">数字员工</span></h1>
        <p class="hero-sub" data-reveal style="--d:450ms">像一名新员工那样接任务、调用工具、产出可直接使用的周报，交付前过自动质检和人工审核；做得不对按意见返工，缺材料或工具故障时停下来说明原因——不编造，不崩溃。</p>
        <div class="hero-rotator" data-reveal style="--d:600ms">
          <div><b>模型</b>只负责分析与写作</div>
          <div><b>程序</b>判定状态流转 · 数字计算 · 质检 · 送审与交付</div>
          <div><b>审核人</b>通过或退回，退回意见被逐条校验</div>
          <div><b>故障</b>重试 3 次仍失败就暂停，人工确认后从断点继续</div>
        </div>
        <div class="hero-cta" data-reveal style="--d:750ms">
          <a class="btn btn-primary" href="#/new" data-magnetic data-cursor="出发">${roll("发起任务")}<span class="arrow">${I.arrow}</span></a>
          <a class="btn btn-ghost" href="#/tasks" data-magnetic>${roll("任务中心")}</a>
        </div>
      </div>
      <div class="hero-visual" data-reveal="scale" style="--d:200ms">
        <canvas id="orb"></canvas>
        <div class="orb-tag" style="left:0;top:16%"><b>${(emp.tools || []).length}</b>个 MCP 工具</div>
        <div class="orb-tag" style="right:0;top:36%"><b>${(emp.quality_checks || []).length}</b>项自动质检</div>
        <div class="orb-tag" style="left:6%;bottom:20%"><b>${Object.keys(meta.states).length}</b>个状态 · 程序闸门</div>
        <div class="orb-state">${active ? `<a href="#/tasks/${esc(active.id)}">${stateChip(active.state, "正在工作 · " + active.label)}</a>` : `<span class="state" data-s="DELIVERED">待命中</span>`}</div>
      </div>
      <div class="scroll-hint">向下滚动<i></i></div>
    </section>
    <div class="marquee tilt" id="marq"><div class="marquee-track"></div></div>

    <section class="section">
      <div class="grid g4">
        ${[["累计任务", cnt.total, "全部发起过的任务", ""], ["已交付", cnt.delivered, "审核通过并生成交付包", "accent"], ["待审核", cnt.review, "质检全部通过，等审核人", ""], ["需人工处理", cnt.attn, "待补材料 / 暂停转人工", ""]]
          .map(([k, v, f, c], i) => `<div class="card spot stat ${c}" data-reveal style="--d:${i * 90}ms" data-tilt="5"><div class="k">${k}<span class="mono faint">0${i + 1}</span></div><div class="v"><span data-count="${v}">0</span></div><div class="foot">${f}</div></div>`).join("")}
      </div>
    </section>

    <section class="section">
      <div class="section-head"><div><div class="eyebrow" data-reveal>评审重点</div><h2 class="section-title" data-split>可控 · 可用 · 懂业务</h2></div></div>
      <div class="grid g3">
        ${[["01", "可控", "能否送审、能否交付由程序闸门判定：只认质检记录和文件哈希。模型不能触发任何状态流转，非法流转被拒绝并留痕。"],
           ["02", "可用", "所有数字由程序按口径计算、正文用占位符引用；六个章节、两张图，Word 不超过 3 页，打开第一页就是结论。"],
           ["03", "懂业务", "对齐《周报格式要求》：按记录 ID 去重、退货冲减、停业与缺数分开说明；纪要说法与数据不一致时以数据为准。"]]
          .map(([n, h, p], i) => `<div class="card spot principle" data-reveal style="--d:${i * 110}ms"><div class="big">${n}</div><div><h3>${h}</h3><p>${p}</p></div></div>`).join("")}
      </div>
    </section>

    <section class="section">
      <div class="section-head"><div><div class="eyebrow" data-reveal>员工配置 · employee.yaml</div><h2 class="section-title" data-split>${steps.length} 步工作法</h2></div>
        <p class="muted" data-reveal style="max-width:420px">程序直接调用工具完成读取、计算、图表与生成；只有原因分析和撰写交给模型，并且每步只开放指定的工具。</p></div>
      <div class="steps-rail"><div class="rail-line"></div>
        ${steps.map((s, i) => `<div class="step-card" data-reveal style="--d:${i * 80}ms"><div class="node ${s.type === "agent" ? "model" : ""}">0${i + 1}</div><h4>${esc(s.name)}</h4><p>${s.type === "agent" ? '<span class="chip model">模型</span>' : '<span class="chip">程序</span>'}</p><p style="margin-top:8px" class="mono">${(s.tools || []).map(esc).join("<br>")}</p></div>`).join("")}
      </div>
    </section>

    <section class="section">
      <div class="section-head"><div><div class="eyebrow" data-reveal>states.py · 流转表</div><h2 class="section-title" data-split>状态机，程序把关</h2></div>
        <p class="muted" data-reveal style="max-width:440px">悬停任一状态查看可走的流转、触发方与闸门条件。表外的流转、触发方不对、闸门不满足的操作一律拒绝，并写入 transition_rejected 事件。</p></div>
      <div class="card" data-reveal="scale"><div id="hgraph"></div>
        <div class="graph-legend"><span><i class="hot"></i>演示路径（含一次自动修正、一次退回返工）</span><span><i></i>正常流转</span><span><i class="dash"></i>暂停 / 缺材料 / 拒绝</span></div></div>
    </section>

    <section class="section">
      <div class="section-head"><div><div class="eyebrow" data-reveal>quality_checks</div><h2 class="section-title" data-split>不通过，就不能送审</h2></div>
        <p class="muted" data-reveal style="max-width:420px">程序能确定修正的先修（如把数字换成占位符），其余交模型改写，最多 ${esc(emp.limits.qc_max_fix_rounds ?? 2)} 轮，仍不通过则暂停转人工。检验记录随交付包保存。</p></div>
      <div class="rule-cloud">${(emp.quality_checks || []).map((q, i) => `<span class="rule-pill" data-reveal style="--d:${i * 50}ms">${esc(q.name)}<span class="mono">${esc(q.rule)}</span></span>`).join("")}
        <span class="rule-pill" data-reveal style="--d:${(emp.quality_checks || []).length * 50}ms">审核意见已采纳<span class="mono">feedback_addressed</span></span></div>
    </section>

    <section class="section">
      <div class="section-head"><div><div class="eyebrow" data-reveal>最近任务</div><h2 class="section-title" data-split>工作记录</h2></div><a class="link-arrow" href="#/tasks">全部任务 ${I.arrow}</a></div>
      <div class="task-grid" id="hrecent"></div>
    </section>

    <footer class="footer">
      <div class="huge" data-reveal><span class="outline-text">不编造，</span><br><span class="gradient-text">不崩溃。</span></div>
      <div class="row"><span>迷你数字员工 · ${esc(emp.name || "")}</span><span class="mono">${S.mock ? "演示数据" : "模型 " + esc(meta.model) + " · 数据目录 " + esc(meta.runs_dir)}</span></div>
    </footer>`;

    const orb = FX.orb($("#orb", scope.root), { n: 1100 });
    scope.on(() => orb.destroy());
    orb.set(active ? "running" : "idle");
    const hosts = $$(".hero-rotator div", scope.root);
    let ri = 0;
    hosts[0].classList.add("on");
    scope.every(() => {
      hosts[ri].classList.remove("on"); hosts[ri].classList.add("off");
      const prev = hosts[ri];
      setTimeout(() => prev.classList.remove("off"), 800);
      ri = (ri + 1) % hosts.length;
      hosts[ri].classList.add("on");
    }, 2800);
    if (active) FX.mood("running");

    // 跑马灯：有指标时展示最近一份周报的数字
    const marq = $("#marq .marquee-track", scope.root);
    const fill = (items) => {
      const one = items.map((t) => `<span class="marquee-item">${t}<i></i></span>`).join("");
      marq.innerHTML = one + one;
    };
    fill(["模型只负责分析和写作", "状态流转由程序判定", "质检不过不能送审", "审核意见逐条落实", "缺材料就暂停", "不编造，不崩溃"]);
    const withK = tasks.find((t) => t.has_artifact);
    if (withK) {
      API.get(`/api/tasks/${withK.id}`).then((d) => {
        if (!scope.alive || !d.kpis) return;
        const k = d.kpis, T = k.total || {};
        fill([`${esc(k.period)} 实收 <em>${Charts.int(T.sales)} 元</em>`, `环比 <em>${Charts.spct(T.sales_wow)}</em>`, `毛利率 <em>${Charts.pct(T.gross_margin)}</em>`, `外卖占比 <em>${Charts.pct(T.delivery_share)}</em>`, `目标达成 <em>${Charts.pct(T.target_rate)}</em>`]
          .concat((k.regions || []).map((r) => `${esc(r.name)} <em>${Charts.spct(r.sales_wow)}</em>`)));
      }).catch(() => {});
    }

    const g = Graph.create($("#hgraph", scope.root), meta, { onStep: (s) => { if (s === "DELIVERED") orb.wave(); } });
    g.demo();
    scope.on(() => g.stopDemo());

    const rec = $("#hrecent", scope.root);
    rec.innerHTML = tasks.slice(0, 6).map(taskCard).join("") || `<div class="empty" style="grid-column:1/-1"><div class="big">还没有任务</div><p>从“发起任务”开始，交给员工第一份周报。</p></div>`;
    bindTaskCards(rec);
  }

  /* ================================================================ 02 发起任务 */
  async function pageNew(scope) {
    const emp = S.meta.employee;
    if (!emp) { location.hash = "#/config"; return; }
    const store = {}, values = {};
    const faultOpts = (S.meta.faults || []).filter((f) => f.scope !== "global");
    const chosen = new Set();
    scope.root.innerHTML = pageHead("02", `发起<span class="hl">任务</span>`, esc(emp.description)) + `
      <div class="grid split-7-5" style="align-items:start">
        <form class="card" id="nform" data-reveal autocomplete="off" novalidate></form>
        <div class="ticket" data-reveal style="--d:200ms"><div class="ticket-body" id="ticket"></div></div>
      </div>`;
    const form = $("#nform", scope.root);
    const ticket = $("#ticket", scope.root);
    let useSample = false;
    emp.form.forEach((f) => {
      if (f.type === "file") {
        form.appendChild(dropZone(f, store, renderTicket));
        if (f.waivable) form.appendChild(waiveToggle(f, values, renderTicket));
        return;
      }
      const box = document.createElement("div");
      box.className = "field";
      if (f.type === "enum") {
        box.innerHTML = `<div class="field-label"><b>${esc(f.label)}${f.required ? '<span class="req">*</span>' : ""}</b><span class="faint">${esc(f.guide || "")}</span></div>
          <div class="seg">${(f.required ? [] : [""]).concat(f.options || []).map((o) => `<button type="button" data-v="${esc(o)}" class="${o === "" ? "on" : ""}">${o === "" ? "不指定" : esc(o)}</button>`).join("")}</div>`;
        values[f.key] = "";
        box.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
          box.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
          values[f.key] = b.dataset.v;
          renderTicket();
        }));
      } else {
        const isWeek = f.type === "iso_week";
        box.innerHTML = `<div class="field-label"><b>${esc(f.label)}${f.required ? '<span class="req">*</span>' : ""}</b>${isWeek ? '<span class="chips" id="wkchips"></span>' : ""}</div>
          <div class="input-line"><input type="text" name="${esc(f.key)}" placeholder="${esc(f.help || "")}" spellcheck="false"></div><div class="hint"></div>`;
        const input = box.querySelector("input"), hint = box.querySelector(".hint"), line = box.querySelector(".input-line");
        values[f.key] = "";
        const check = () => {
          values[f.key] = input.value.trim();
          if (isWeek) {
            const w = isoWeek(values[f.key]);
            line.classList.toggle("bad", !!values[f.key] && (!w || !!w.bad));
            hint.className = "hint " + (!values[f.key] ? "" : w && !w.bad ? "ok" : "bad");
            hint.textContent = !values[f.key] ? "ISO 周（周一至周日），例如 2026-W44、2026年第44周、2026-10-26~2026-11-01" : w && !w.bad ? "✓ " + w.label : (w && w.bad) || "格式应为 2026-W44、2026年第44周 或 2026-10-26~2026-11-01";
          }
          renderTicket();
        };
        input.addEventListener("input", check);
        if (isWeek) {
          const chips = box.querySelector("#wkchips");
          chips.innerHTML = ["2026-W44", "2026-W43", "2026-W42"].map((w) => `<button type="button" class="chip chip-btn mono">${w}</button>`).join("");
          chips.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { input.value = b.textContent; check(); input.focus(); }));
        }
        setTimeout(check, 0);
      }
      form.appendChild(box);
    });
    const extra = document.createElement("div");
    extra.innerHTML = `${S.meta.sample_available ? `<label class="toggle" style="margin:6px 0 22px"><input type="checkbox" id="nsample"><span class="sw"></span><span>未上传的文件使用官方样例数据<span class="faint">（销售明细.xlsx + 第 44 周纪要）</span></span></label>` : ""}
      <details class="fault-panel"><summary><span>${I.bolt} 演示：只对本任务注入故障</span><span class="pm">+</span></summary>
        <div class="fp-body"><p class="faint" style="margin:0 0 12px;font-size:13px">工具网关按注入的故障返回超时或错误；用于现场演示“重试 → 暂停 → 从断点继续”。</p>
        <div class="chips">${faultOpts.map((f) => `<button type="button" class="chip chip-btn" data-f="${esc(f.id)}">${esc(f.label)}</button>`).join("")}</div></div></details>
      <div style="display:flex;gap:12px;align-items:center;margin-top:28px;flex-wrap:wrap">
        <button type="submit" class="btn btn-primary" id="nsubmit" data-magnetic=".25">${roll("提交任务")}<span class="arrow">${I.arrow}</span></button>
        <span class="faint" style="font-size:13px">提交后由程序做准入校验：缺材料会暂停并提示补充。</span></div>`;
    form.appendChild(extra);
    const sample = $("#nsample", form);
    if (sample) sample.addEventListener("change", () => { useSample = sample.checked; renderTicket(); });
    $$("[data-f]", form).forEach((b) => b.addEventListener("click", () => {
      const id = b.dataset.f;
      chosen.has(id) ? chosen.delete(id) : chosen.add(id);
      b.classList.toggle("on", chosen.has(id));
      if (chosen.has(id)) FX.glitch();
      renderTicket();
    }));

    function renderTicket() {
      const w = isoWeek(values.period);
      const waived = (k) => values["waive_" + k] === "1" && !(store[k] && store[k].length);
      const fileTxt = (k) => (store[k] && store[k].length ? store[k].map((f) => f.name).join("、") : waived(k) ? "已确认没有" : useSample && (k === "sales_file" || k === "minutes_file") ? "官方样例" : "未上传");
      const has = (k) => (store[k] && store[k].length) || waived(k) || (useSample && (k === "sales_file" || k === "minutes_file"));
      const req = emp.form.filter((f) => f.type === "file" && f.required);
      ticket.innerHTML = `<div class="t-eyebrow"><span>任务单 · ${esc(emp.name)}</span><span>${S.role === "reviewer" ? "审核人" : "发起人"}</span></div>
        <div class="t-period">${w && !w.bad ? "W" + w.w : "W—"}</div>
        <div class="t-label">${w && !w.bad ? esc(w.label) : "填写统计周期"}</div>
        <div class="t-rows">${emp.form.filter((f) => f.key !== "period").map((f) => `<div class="t-row"><span>${esc(f.label)}</span><span>${esc(f.type === "file" ? fileTxt(f.key) : values[f.key] || "—")}</span></div>`).join("")}
          <div class="t-row"><span>注入故障</span><span>${chosen.size ? esc([...chosen].map((id) => faultOpts.find((f) => f.id === id).label).join("、")) : "无"}</span></div></div>
        <div class="t-cut"></div>
        <div class="t-checks">
          <div class="t-check ${w && !w.bad ? "ok" : ""}"><i>${w && !w.bad ? "✓" : "·"}</i>统计周期格式</div>
          ${req.map((f) => `<div class="t-check ${has(f.key) ? "ok" : "warn"}"><i>${has(f.key) ? "✓" : "!"}</i>${esc(f.label)}${has(f.key) ? "" : "：缺少时任务会暂停待补"}</div>`).join("")}
          <div class="t-check"><i>→</i>列、周次覆盖与数据质量由程序校验</div>
        </div>
        <div class="barcode"></div><div class="t-id">T${new Date().toISOString().slice(5, 10).replace("-", "")}-······-····</div>`;
    }
    renderTicket();

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = $("#nsubmit", form);
      if (S.role !== "initiator") { FX.toast("只有发起人可以发起任务，请先在右上角切换为“发起人”", "err"); return; }
      const body = { values: Object.fromEntries(Object.entries(values).filter(([, v]) => v)), use_sample: useSample, faults: [...chosen] };
      busy(btn, true);
      try {
        body.files = await packFiles(store);
        const r = await API.post("/api/tasks", body);
        const [x, y] = centerOf(ticket);
        FX.burst(x, y, { n: 120 });
        FX.pulse(1);
        $(".ticket", scope.root).classList.add("printing");
        FX.toast(`已创建任务 ${r.id}，员工开始工作`, "ok");
        scope.later(() => { location.hash = `#/tasks/${r.id}`; }, 900);
      } catch (err) {
        FX.toast(err.message || String(err), "err", 6500);
        const [x, y] = centerOf(btn);
        FX.shock(x, y);
        busy(btn, false);
      }
    });
  }

  /* ================================================================ 03 任务中心 */
  function taskCard(t) {
    const p = PROGRESS[t.state] ?? 0.3;
    const yw = String(t.period || "").match(/(\d{4})\D*?(\d{1,2})\D*$/);
    return `<a class="card spot task-card" href="#/tasks/${esc(t.id)}" data-s="${esc(t.state)}" data-tilt="4" data-cursor="查看">
      <i class="tc-glow"></i>
      <div class="tc-top">${stateChip(t.state, t.label)}<span class="tc-id">${esc(t.id)}</span></div>
      <div><div class="tc-period">${yw ? "W" + +yw[2] : esc(t.period || "—")}</div><div class="faint" style="font-size:13px">${yw ? yw[1] + " 年第 " + +yw[2] + " 周" : "统计周期"}</div></div>
      <div class="tc-reason">${esc(t.pause_reason || (t.state === "DELIVERED" ? "审核通过，已生成交付包" : t.state === "PENDING_REVIEW" ? "质检全部通过，等待审核" : AUTO.includes(t.state) ? "员工正在工作…" : ""))}</div>
      <div class="progress-mini"><i style="transform:scaleX(${p})"></i></div>
      <div class="tc-foot"><span>V${t.version} · 更新 ${esc(short(t.updated_at))}</span><span>查看 →</span></div></a>`;
  }
  function bindTaskCards(root) { FX.bind(root); }

  async function pageTasks(scope) {
    let filter = "all";
    const groups = {
      all: ["全部", () => true], run: ["进行中", (t) => AUTO.includes(t.state)],
      wait: ["等人处理", (t) => ["WAITING_INPUT", "SUSPENDED", "PENDING_REVIEW"].includes(t.state)], end: ["已结束", (t) => TERMINAL.includes(t.state)],
    };
    scope.root.innerHTML = pageHead("03", `任务<span class="hl">中心</span>`, "每个任务都是一份可追溯的工作记录：状态、步骤、质检、审核和交付文件。",
      `<a class="btn btn-primary" href="#/new" data-magnetic>${roll("发起任务")}<span class="arrow">${I.arrow}</span></a>`) +
      `<div class="filters" id="tfil"></div><div class="task-grid" id="tgrid"></div>`;
    let last = "";
    const draw = (tasks) => {
      const sig = JSON.stringify(tasks.map((t) => [t.id, t.state, t.version, t.updated_at])) + filter;
      $("#tfil", scope.root).innerHTML = Object.entries(groups).map(([k, [lab, fn]]) => `<button class="chip chip-btn ${filter === k ? "on" : ""}" data-k="${k}">${lab} <span class="mono">${tasks.filter(fn).length}</span></button>`).join("");
      $$("#tfil button", scope.root).forEach((b) => b.addEventListener("click", () => { filter = b.dataset.k; last = ""; draw(S.tasks); }));
      if (sig === last) return;
      last = sig;
      const list = tasks.filter(groups[filter][1]);
      const grid = $("#tgrid", scope.root);
      grid.innerHTML = list.map(taskCard).join("") || `<div class="empty" style="grid-column:1/-1"><div class="big">这里还空着</div><p>没有符合条件的任务。</p><a class="btn btn-ghost" href="#/new">${roll("发起任务")}</a></div>`;
      $$(".task-card", grid).forEach((c, i) => { c.setAttribute("data-reveal", ""); c.style.setProperty("--d", `${Math.min(i, 10) * 50}ms`); });
      FX.bind(grid);
    };
    S.tasks = await API.get("/api/tasks");
    draw(S.tasks);
    scope.poll(async () => { S.tasks = await API.get("/api/tasks"); draw(S.tasks); }, 2500);
  }

  /* ================================================================ 任务详情 */
  async function pageTask(scope, id) {
    let t = await API.get(`/api/tasks/${id}`);
    let events = await API.get(`/api/tasks/${id}/events`);
    const root = scope.root;
    root.innerHTML = `
      <a class="link-arrow" href="#/tasks">← 全部任务</a>
      <div class="detail-head" style="margin-top:18px">
        <div>
          <div class="eyebrow" data-reveal>任务 <b class="mono">${esc(t.id)}</b></div>
          <div class="period" data-split>${esc(t.period || "—")}</div>
          <div class="detail-meta" id="dmeta"></div>
        </div>
        <div style="width:200px;height:200px;justify-self:end" data-reveal="scale"><canvas id="torb" style="width:100%;height:100%"></canvas></div>
      </div>
      <div id="dalert"></div>
      <div id="dact"></div>
      <div id="dedit"></div>
      <div class="card" data-reveal style="margin-top:18px"><div class="row-between"><div class="card-title"><span class="dot"></span>工作步骤</div><span class="faint" style="font-size:12px">完成的步骤落盘并记录哈希，断点恢复时跳过</span></div><div class="pipeline" id="dpipe" style="--n:${t.steps.length}"></div></div>
      <div class="card" id="dlivecard" style="margin-top:18px;display:none"><div class="row-between"><div class="card-title"><span class="dot"></span>实时进度 · 模型输出</div><span class="faint" style="font-size:12px" id="dlconn">实时推送</span></div><div class="live-nodes" id="dlnodes"></div><div id="dlmodels"></div></div>
      <div class="card" data-reveal style="margin-top:18px"><div class="row-between"><div class="card-title"><span class="dot"></span>状态轨迹</div><a class="link-arrow" href="#/events/${esc(id)}">完整回放 ${I.arrow}</a></div><div id="dgraph"></div></div>
      <div class="card" data-reveal style="margin-top:18px"><div class="row-between"><div class="card-title"><span class="dot"></span>实时事件 · 只追加</div><span class="faint mono" style="font-size:12px" id="dcount"></span></div><div class="feed" id="dfeed"></div></div>
      <div id="dkpi"></div>`;
    const orb = FX.orb($("#torb", root), { n: 700, radius: 0.3 });
    scope.on(() => orb.destroy());
    const graph = Graph.create($("#dgraph", root), S.meta);
    graph.setPath(Graph.pathFromEvents(events), t.state);
    let lastSeq = 0, prevState = null, kpiDone = false;

    const renderMeta = () => {
      $("#dmeta", root).innerHTML = `${stateChip(t.state, t.label, true)}<span class="chip mono">V${t.version}</span>
        <span class="chip">返工 ${t.rework_rounds}/${(S.meta.employee && S.meta.employee.limits.rework_max_rounds) || 3}</span>
        <span class="chip">本版自动修正 ${t.qc_rounds}/${(S.meta.employee && S.meta.employee.limits.qc_max_fix_rounds) || 2}</span>
        <span class="chip">发起于 ${esc(short(t.created_at))}</span>
        ${Object.entries(t.file_names || {}).map(([k, v]) => `<span class="chip" title="${esc(k)}">${I.clip} ${esc([].concat(v).join("、"))}</span>`).join("")}
        ${(t.faults || []).map((f) => `<span class="chip down">${I.boltSm} ${esc(f.tool)}:${esc(f.mode)}</span>`).join("")}`;
    };
    const renderAlert = () => {
      const a = $("#dalert", root);
      const map = {
        WAITING_INPUT: ["warn", "!", "待补材料", t.pause_reason],
        SUSPENDED: ["danger", "!", "暂停转人工", t.pause_reason],
        REJECTED: ["danger", "×", "已拒绝", t.pause_reason || "输入不合规，任务已结束"],
        CANCELLED: ["warn", "–", "已取消", "发起人取消了任务"],
        PENDING_REVIEW: ["info", "i", "质检全部通过，等待审核", "能否进入审核由程序读取检验记录判定；审核通过时还会比对审核版本与交付文件的哈希。"],
        DELIVERED: ["ok", "✓", "已交付", "审核通过，交付包含 Word 周报、质检记录与文件清单。"],
      };
      const m = map[t.state];
      a.innerHTML = m ? `<div class="alert ${m[0]}" style="margin-top:6px"><span class="a-ico">${m[1]}</span><h4>${esc(m[2])}</h4><p class="atext"></p></div>` : "";
      if (m) FX.type(a.querySelector(".atext"), m[3] || "", 90);
    };
    const renderActions = () => {
      const box = $("#dact", root);
      box.innerHTML = "";
      const card = document.createElement("div");
      card.className = "card";
      card.style.marginTop = "18px";
      const emp = S.meta.employee;
      if (t.state === "WAITING_INPUT" && emp && S.role !== "initiator") {
        card.innerHTML = `<div class="card-title"><span class="dot" style="background:var(--amber)"></span>补充材料</div>
          <p class="muted" style="margin:0">补充材料、修改统计周期需由发起人操作，可在右上角切换为“发起人”。</p>`;
        box.appendChild(card);
      } else if (t.state === "WAITING_INPUT" && emp) {
        const store = {};
        card.innerHTML = `<div class="card-title"><span class="dot" style="background:var(--amber)"></span>补充材料</div><p class="muted" style="margin-top:0;font-size:14px">销售明细可只上传缺少的部分（如上周数据），会与已上传的数据自动合并。补充后从暂停处继续，不需要重新发起。</p><div class="grid g2" id="supz"></div>
          <button class="btn btn-primary" id="supbtn">${roll("提交补充材料")}<span class="arrow">${I.arrow}</span></button>`;
        const fields = {};
        emp.form.filter((f) => f.type === "iso_week").forEach((f) => {
          const el = document.createElement("div");
          el.className = "field";
          el.style.gridColumn = "1/-1";
          el.innerHTML = `<div class="field-label"><b>${esc(f.label)}</b><span class="faint">填错了可直接修改，修改后重新准入校验</span></div>
            <div class="input-line"><input type="text" spellcheck="false" value="${esc(t.form && t.form[f.key] || t.period || "")}"></div><div class="hint"></div>`;
          const input = el.querySelector("input"), hint = el.querySelector(".hint"), line = el.querySelector(".input-line");
          const orig = input.value;
          input.addEventListener("input", () => {
            const v = input.value.trim(), w = isoWeek(v);
            if (v && v !== orig) fields[f.key] = v; else delete fields[f.key];
            line.classList.toggle("bad", !!v && (!w || !!w.bad));
            hint.className = "hint " + (!v || v === orig ? "" : w && !w.bad ? "ok" : "bad");
            hint.textContent = !v || v === orig ? "" : w && !w.bad ? "✓ 改为 " + w.label : (w && w.bad) || "格式应为 2026-W44、2026年第44周 或 2026-10-26~2026-11-01";
          });
          $("#supz", card).appendChild(el);
        });
        emp.form.filter((f) => f.type === "file").forEach((f) => $("#supz", card).appendChild(dropZone(f, store)));
        emp.form.filter((f) => f.waivable).forEach((f) => $("#supz", card).after(waiveToggle(f, fields)));
        $("#supbtn", card).addEventListener("click", async (e) => {
          const btn = e.currentTarget;
          const ok = await act(btn, async () => API.post(`/api/tasks/${id}/supplement`, { files: await packFiles(store), fields, role: S.role }), fields.period ? "已修改统计周期并提交，重新准入" : "已提交补充材料，重新准入");
          if (ok) refresh();
        });
        box.appendChild(card);
      } else if (t.state === "SUSPENDED" && t.feedback_stuck) {
        card.innerHTML = `<div class="card-title"><span class="dot" style="background:var(--rose)"></span>审核意见无法落实</div>
          <p class="muted" style="margin-top:0;font-size:14px">员工无法按当前意见完成修改。如果意见写错了或不适用，审核人可以更正意见（按新意见重做，不计返工次数），或撤回意见（当前版本重新质检）；原意见留痕。</p>
          ${S.role === "reviewer" ? `<textarea class="textarea" id="fbtxt">${esc(t.feedback_comments || "")}</textarea><div id="fbcheck"></div>
          <div class="btns" style="margin-top:12px;display:flex;gap:12px;flex-wrap:wrap"><button class="btn btn-primary" id="fbrev">${roll("按更正后的意见重做")}</button>
          <button class="btn btn-ghost" id="fbwd">${roll("撤回意见，重新质检")}</button></div>`
          : `<p class="muted" style="font-size:14px">更正或撤回意见需由审核人操作，可在右上角切换身份。</p>`}`;
        const send = async (btn, comments, confirm, okMsg) => {
          busy(btn, true);
          try {
            await API.post(`/api/tasks/${id}/revise_feedback`, { comments, confirm, role: S.role });
            FX.toast(okMsg, "ok");
            refresh();
          } catch (err) {
            busy(btn, false);
            if (err.data && err.data.kind === "feedback_check") feedbackCheckBox($("#fbcheck", card), err.data.issues || [], (b) => send(b, comments, true, okMsg));
            else FX.toast(err.message, "err", 7000);
          }
        };
        if (S.role === "reviewer") {
          $("#fbrev", card).addEventListener("click", (e) => {
            const v = $("#fbtxt", card).value.trim();
            if (!v) { FX.toast("请填写更正后的意见；如要撤回意见，请点“撤回意见”", "warn"); return; }
            send(e.currentTarget, v, false, "已更正审核意见，员工正在按新意见重做");
          });
          $("#fbwd", card).addEventListener("click", (e) => send(e.currentTarget, "", false, "已撤回审核意见，当前版本重新质检"));
          $("#fbtxt", card).addEventListener("input", () => { $("#fbcheck", card).innerHTML = ""; });
        }
        box.appendChild(card);
      } else if (t.state === "SUSPENDED") {
        card.innerHTML = `<div class="card-title"><span class="dot" style="background:var(--rose)"></span>人工处理</div>
          <label class="toggle" style="margin-bottom:18px"><input type="checkbox" id="clr" checked><span class="sw"></span><span>同时清除本任务的故障注入<span class="faint">（模拟服务恢复）</span></span></label>
          <div style="display:flex;gap:12px;flex-wrap:wrap"><button class="btn btn-primary" id="retry" data-magnetic=".2">${roll("从断点重试")}<span class="arrow">${I.restart}</span></button>
          ${t.has_artifact ? `<button class="btn btn-ghost" id="recheck">${roll("人工确认后重新质检")}</button>` : ""}</div>`;
        $("#retry", card).addEventListener("click", async (e) => {
          const ok = await act(e.currentTarget, () => API.post(`/api/tasks/${id}/retry`, { role: S.role, clear_faults: $("#clr", card).checked }), "从断点继续执行，已完成的步骤不会重跑");
          if (ok) { FX.pulse(0.8); orb.wave("62,240,224"); refresh(); }
        });
        const rc = $("#recheck", card);
        if (rc) rc.addEventListener("click", async (e) => { if (await act(e.currentTarget, () => API.post(`/api/tasks/${id}/recheck`, { role: S.role }), "已重新送入自动质检")) refresh(); });
        box.appendChild(card);
      } else if (t.state === "PENDING_REVIEW") {
        card.innerHTML = `<div class="row-between"><div><div class="card-title"><span class="dot" style="background:var(--violet)"></span>等待审核</div><p class="muted" style="margin:0">${S.role === "reviewer" ? "你是审核人，可以直接进入审核台。" : "审核需由审核人操作，可在右上角切换身份。"}</p></div>
          <a class="btn btn-violet" href="#/review/${esc(id)}" data-magnetic>${roll("去审核")}<span class="arrow">${I.arrow}</span></a></div>`;
        box.appendChild(card);
      } else if (t.state === "DELIVERED") {
        card.innerHTML = `<div class="row-between"><div class="card-title"><span class="dot"></span>交付包</div><a class="link-arrow" href="#/review/${esc(id)}">查看周报 ${I.arrow}</a></div>
          <div class="dl-list">${(t.deliverables || []).map((f) => { const ext = f.name.split(".").pop().toLowerCase(); return `<a class="dl-item" ${f.url ? `href="${esc(f.url)}&download=1"` : `href="#" data-nodl`} data-cursor="下载"><span class="ext ${esc(ext)}">${esc(ext)}</span><div><div class="nm">${esc(f.name)}</div><div class="sz">${kb(f.size)}</div></div><span class="go">${I.download}</span></a>`; }).join("")}</div>`;
        $$("[data-nodl]", card).forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); FX.toast("演示数据没有实际文件，连接后端后可下载", "info"); }));
        box.appendChild(card);
      }
      if (EDITABLE.includes(t.state) && t.has_artifact) {
        const ce = document.createElement("div");
        ce.className = "card";
        ce.style.marginTop = "14px";
        ce.innerHTML = `<div class="row-between" style="gap:16px;flex-wrap:wrap"><div><div class="card-title"><span class="dot" style="background:var(--aqua)"></span>人工修改报告</div>
          <p class="muted" style="margin:0;font-size:14px">直接在网页上修改报告文字，提交后生成新版本，仍须自动质检通过，再由审核人审核。</p></div>
          <button class="btn btn-ghost" id="openedit">${roll("在线修改报告")}<span class="arrow">${I.arrow}</span></button></div>`;
        $("#openedit", ce).addEventListener("click", openEditor);
        box.appendChild(ce);
      }
      if (!TERMINAL.includes(t.state)) {
        const c2 = document.createElement("div");
        c2.style.cssText = "margin-top:14px;display:flex;justify-content:flex-end";
        c2.innerHTML = `<button class="btn btn-danger btn-sm" id="cancel">长按 1 秒取消任务</button>`;
        box.appendChild(c2);
        FX.hold($("#cancel", c2), 1000, async () => { if (await act($("#cancel", c2), () => API.post(`/api/tasks/${id}/cancel`, { role: S.role }), "任务已取消")) refresh(); });
      }
      FX.bind(box);
    };
    // 人工在线修改报告：编辑占位符原文，实时预览替换后的文字，提交后新版本重新质检
    async function openEditor() {
      const host = $("#dedit", root);
      let data;
      try { data = await API.get(`/api/tasks/${id}/content`); } catch (e) { FX.toast(e.message, "err"); return; }
      if (!data.editable) { FX.toast("当前状态不能修改报告", "warn"); return; }
      const c = JSON.parse(JSON.stringify(data.content));
      c.anomalies = c.anomalies || [];
      const AN = [["phenomenon", "现象"], ["cause", "原因"], ["suggestion", "建议"]];
      const para = (key, i, text, label) => `<div class="ed-item" data-k="${esc(key)}" data-i="${i}"${label ? ` data-f="${esc(label)}"` : ""}>
          <textarea class="textarea" rows="2" style="min-height:64px">${esc(text)}</textarea>
          <div class="ed-prev faint" style="font-size:13px;margin:6px 2px 0"></div>
          ${label ? "" : `<button class="btn btn-ghost btn-sm ed-del" style="margin-top:6px">删除这一段</button>`}</div>`;
      const secHtml = (s) => `<div class="ed-sec" data-sec="${esc(s.id)}" style="margin-top:22px"><h4 style="margin:0 0 10px">${esc(s.title)}<span class="faint" style="font-weight:400;font-size:12px;margin-left:8px">${s.kind === "bullets" ? "每条一个要点" : "每段一个文本框"}</span></h4>
          <div class="ed-list" style="display:grid;gap:12px">${(c[s.id] || []).map((x, i) => para(s.id, i, x)).join("")}</div>
          <button class="btn btn-ghost btn-sm ed-add" data-sec="${esc(s.id)}" style="margin-top:10px">＋ 添加一段</button></div>`;
      const anHtml = () => c.anomalies.length ? `<div style="margin-top:22px"><h4 style="margin:0 0 10px">${esc(data.anomaly_section)}</h4>
          ${c.anomalies.map((a, i) => `<div style="margin-bottom:16px"><div class="chip" style="margin-bottom:8px">${esc(a.id)} · ${esc(data.anomaly_labels[a.id] || "")}</div>
            <div style="display:grid;gap:10px">${AN.map(([f, lab]) => `<div><div class="faint" style="font-size:12px;margin-bottom:4px">${lab}</div>${para("anomalies", i, a[f] || "", f)}</div>`).join("")}</div></div>`).join("")}</div>` : "";
      host.innerHTML = `<div class="card" style="margin-top:14px"><div class="row-between"><div class="card-title"><span class="dot" style="background:var(--aqua)"></span>在线修改报告 · 当前版本 ${esc(data.tag || "")}</div>
          <button class="btn btn-ghost btn-sm" id="edclose">收起</button></div>
        <p class="muted" style="margin:0;font-size:14px"><code>{{…}}</code> 是程序计算的数字，生成 Word 时自动替换（下方灰字为替换后的预览）。请保留占位符，不要手写数字；不存在的占位符会标红。表格、图表和数据质量说明由程序生成，不在这里修改。</p>
        <div id="edmiss"></div>
        <div id="edbody">${data.sections.map(secHtml).join("")}${anHtml()}</div>
        <div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:22px;align-items:center"><button class="btn btn-primary" id="edsubmit">${roll("提交修改，重新质检")}<span class="arrow">${I.arrow}</span></button>
          <span class="faint" style="font-size:13px">提交后生成新版本 → 自动质检 → 通过后进入审核</span></div></div>`;
      const body = $("#edbody", host);
      const collect = () => {
        const out = JSON.parse(JSON.stringify(c));
        data.sections.forEach((s) => { out[s.id] = $$(`.ed-item[data-k="${s.id}"] textarea`, body).map((x) => x.value); });
        $$('.ed-item[data-k="anomalies"]', body).forEach((el) => { out.anomalies[+el.dataset.i][el.dataset.f] = $("textarea", el).value; });
        return out;
      };
      let timer = null;
      const preview = () => {
        clearTimeout(timer);
        timer = setTimeout(async () => {
          let r;
          try { r = await API.post(`/api/tasks/${id}/preview`, { content: collect() }); } catch (e) { return; }
          $$(".ed-item", body).forEach((el) => {
            const k = el.dataset.k, i = +el.dataset.i, f = el.dataset.f;
            const txt = k === "anomalies" ? ((r.anomalies[(c.anomalies[i] || {}).id] || {})[f] || "") : ((r.texts[k] || [])[i] || "");
            $(".ed-prev", el).textContent = txt ? "预览：" + txt : "";
          });
          $("#edmiss", host).innerHTML = r.missing.length ? `<div class="alert danger" style="margin-top:12px"><span class="a-ico">!</span><h4>以下占位符不存在，提交后质检不会通过</h4><p>${r.missing.map((m) => `<code>{{${esc(m)}}}</code>`).join("　")}</p></div>` : "";
        }, 350);
      };
      const renumber = (sec) => $$(`.ed-item[data-k="${sec}"]`, body).forEach((el, i) => { el.dataset.i = i; });
      body.addEventListener("input", preview);
      body.addEventListener("click", (e) => {
        const del = e.target.closest(".ed-del"), add = e.target.closest(".ed-add");
        if (del) { const it = del.closest(".ed-item"), k = it.dataset.k; it.remove(); renumber(k); preview(); }
        if (add) {
          const k = add.dataset.sec, list = add.previousElementSibling;
          list.insertAdjacentHTML("beforeend", para(k, list.children.length, ""));
          $("textarea", list.lastElementChild).focus();
        }
      });
      $("#edclose", host).addEventListener("click", () => { host.innerHTML = ""; });
      $("#edsubmit", host).addEventListener("click", async (e) => {
        const ok = await act(e.currentTarget, () => API.post(`/api/tasks/${id}/recheck`, { content: collect(), role: S.role }), "已提交修改，新版本正在重新质检");
        if (ok) { host.innerHTML = ""; refresh(); }
      });
      preview();
      host.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    const renderPipe = () => {
      const halted = t.state === "SUSPENDED" || t.state === "WAITING_INPUT";
      $("#dpipe", root).innerHTML = t.steps.map((s, i) => {
        const cls = s.status === "running" ? (halted ? "halted" : "running") : s.status === "done" ? "done" : "pending";
        return `<div class="pipe-step ${cls}"><div class="ps-n"><span>0${i + 1}</span><b>${s.status === "done" ? "✓" : s.status === "running" ? (halted ? "!" : "…") : ""}</b></div><div class="ps-name">${esc(s.name)}</div><div class="ps-type">${s.type === "agent" ? "模型 · 只开放指定工具" : s.type === "render" ? "程序 · 生成文件" : "程序 · 调用工具"}</div><i class="ps-bar"></i></div>`;
      }).join("");
    };
    /* 实时进度：服务端经 SSE 推送正在运行的节点与模型流式输出（执行任务的进程写入 SQLite，网页进程读出推送） */
    const live = {};
    const NODE_NAMES = { created: "创建", admission: "准入校验", running: "执行步骤", rework: "返工", qc: "质检", auto_fix: "自动修正", finish: "汇总产物", model: "模型生成", tools: "调用工具", parse: "校验输出" };
    const nodeName = (p) => NODE_NAMES[p] || ((t.steps.find((s) => s.id === p) || {}).name) || p;
    const renderLive = () => {
      const card = $("#dlivecard", root);
      const running = (live.nodes && live.nodes.running) || [];
      const EXTRA = { fix: "自动修正", verify: "复核员 · 核对原因" };  // 不在步骤列表里的模型调用
      const models = [...t.steps, ...Object.entries(EXTRA).map(([id, name]) => ({ id, name }))]
        .filter((s) => live["model:" + s.id]).map((s) => [s, live["model:" + s.id]]);
      card.style.display = running.length || models.length ? "" : "none";
      $("#dlnodes", root).innerHTML = running.length
        ? running.map((l) => `<span class="chip"><i></i>${esc(l.split(" › ").map(nodeName).join(" › "))}</span>`).join("")
        : `<span class="faint" style="font-size:12px">${AUTO.includes(t.state) ? "等待下一个节点…" : "当前没有运行中的节点"}</span>`;
      const box = $("#dlmodels", root);
      models.forEach(([s, m]) => {
        let el = box.querySelector(`[data-step="${s.id}"]`);
        if (!el) {
          el = document.createElement("div");
          el.className = "live-model";
          el.dataset.step = s.id;
          el.innerHTML = `<div class="lm-head"><b></b><span class="mono"></span></div><pre></pre>`;
          box.appendChild(el);
        }
        const on = !m.done && AUTO.includes(t.state);
        el.classList.toggle("on", on);
        el.querySelector("b").textContent = `${s.name} · 第 ${m.turn} 轮`;
        el.querySelector(".mono").textContent = on ? "生成中…" : (m.tool_calls && m.tool_calls.length ? "调用工具：" + m.tool_calls.join("、") : "本轮完成");
        const pre = el.querySelector("pre");
        const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
        pre.textContent = m.text || (m.tool_calls && m.tool_calls.length ? "（本轮只调用工具，没有文字输出）" : "");
        if (stick) pre.scrollTop = pre.scrollHeight;
      });
    };
    const feed = $("#dfeed", root);
    const pushEvents = (list, animate) => {
      list.forEach((e) => {
        const row = feedRow(e);
        if (!animate) row.style.animation = "none";
        feed.insertBefore(row, feed.firstChild);
      });
      while (feed.children.length > 80) feed.removeChild(feed.lastChild);
      $("#dcount", root).textContent = `${events.length} 条事件`;
    };
    const renderKpi = () => {
      if (kpiDone || !t.kpis) return;
      kpiDone = true;
      const k = t.kpis, T = k.total || {};
      const host = $("#dkpi", root);
      host.innerHTML = `<section class="section" style="margin-top:80px">
        <div class="section-head"><div><div class="eyebrow" data-reveal>compute_kpis · 程序计算</div><h2 class="section-title" data-split>本周经营数据</h2></div>
          <p class="muted" data-reveal style="max-width:440px">${esc(k.period_label || "")}。所有数字由工具按《周报格式要求》口径算出，周报正文只能通过占位符引用，模型不做任何计算。</p></div>
        <div class="kpi-row">
          ${[["本周实收（元）", T.sales, "int", null], ["环比", T.sales_wow, "spct1", T.sales_wow], ["毛利率", T.gross_margin, "pct1", null], ["外卖占比", T.delivery_share, "pct1", null], ["目标达成率", T.target_rate, "pct1", null]]
            .map(([lab, v, f, d], i) => `<div class="card spot kpi" data-reveal style="--d:${i * 70}ms"><div class="k">${lab}</div><div class="v" ${v == null ? "" : `data-count="${v}" data-fmt="${f}"`} style="${d != null ? `color:${d < 0 ? "var(--rose)" : "var(--lime)"}` : ""}">${v == null ? "—" : "0"}</div></div>`).join("")}
        </div>
        <div class="grid g2" style="margin-top:18px">
          <div class="card" data-reveal><div class="card-title"><span class="dot"></span>区域实收与目标达成</div><div id="kreg"></div></div>
          <div class="card" data-reveal style="--d:100ms"><div class="card-title"><span class="dot"></span>近 4 周实收趋势</div><div id="ktrend"></div></div>
        </div>
        <div class="grid g2" style="margin-top:18px">
          <div class="card" data-reveal><div class="card-title"><span class="dot" style="background:var(--rose)"></span>环比超过 ±${Math.round((k.threshold || 0.1) * 100)}% 的对象</div><div id="kanom"></div></div>
          <div class="card" data-reveal style="--d:100ms"><div class="card-title"><span class="dot"></span>品类结构</div><div id="kcat"></div></div>
        </div>
        <div class="card" data-reveal style="margin-top:18px"><div class="card-title"><span class="dot"></span>每日实收 · 本周对比上周</div><div id="kday"></div></div>
        <div class="card" data-reveal style="margin-top:18px"><div class="card-title"><span class="dot"></span>本周实收前 ${(k.top_items || []).length} 名单品</div><div class="chips">${(k.top_items || []).map((n, i) => `<span class="chip" style="font-size:14px;padding:8px 14px"><span class="mono faint">${i + 1}</span> ${esc(n)}</span>`).join("")}</div></div>
      </section>`;
      Charts.regions($("#kreg", host), k.regions || []);
      Charts.trend($("#ktrend", host), k.weekly || []);
      Charts.diverging($("#kanom", host), k.anomalies || []);
      Charts.donut($("#kcat", host), k.categories || []);
      Charts.daily($("#kday", host), k.daily || []);
      FX.bind(host);
    };
    const onState = (initial) => {
      renderMeta(); renderAlert(); renderActions();
      FX.mood(MOOD_OF[t.state] || "idle");
      orb.set(ORB_OF[t.state] || "idle");
      if (initial) return;
      if (!EDITABLE.includes(t.state)) $("#dedit", root).innerHTML = "";
      const chip = $("#dmeta .state", root);
      if (chip) FX.scramble(chip, t.label, 600);
      if (t.state === "DELIVERED") { FX.burst(innerWidth / 2, innerHeight / 3, { n: 180 }); FX.pulse(1); orb.wave(); FX.toast("审核通过，已交付", "ok"); }
      else if (t.state === "SUSPENDED") { FX.glitch(); orb.wave("255,92,122"); FX.toast("任务已暂停，需要人工处理", "err"); }
      else if (t.state === "WAITING_INPUT") { orb.wave("255,181,71"); FX.toast("任务暂停：需要补充材料", "warn"); }
      else if (t.state === "PENDING_REVIEW") { FX.pulse(0.9); orb.wave("139,108,255"); FX.toast("质检全部通过，已送审", "ok"); }
      else if (t.state === "AUTO_FIXING") FX.toast("质检未通过，进入自动修正", "warn", 3000);
    };
    lastSeq = events.length ? events[events.length - 1].seq : 0;
    pushEvents(events.slice(-40), false);
    renderPipe();
    onState(true);
    scope.listen(window, "rolechange", renderActions);
    prevState = t.state;
    renderKpi();

    async function doRefresh() {
      const [nt, ne] = await Promise.all([API.get(`/api/tasks/${id}`), API.get(`/api/tasks/${id}/events`)]);
      if (!scope.alive) return;
      t = nt; events = ne;
      const fresh = events.filter((e) => e.seq > lastSeq);
      if (fresh.length) {
        lastSeq = fresh[fresh.length - 1].seq;
        pushEvents(fresh, true);
        const moves = fresh.filter((e) => e.type === "state_changed");
        moves.forEach((e, i) => scope.later(() => graph.comet(e.payload.from, e.payload.to, 650), i * 380));
        scope.later(() => graph.setPath(Graph.pathFromEvents(events), t.state), moves.length * 380 + 600);
      }
      renderPipe();
      renderLive();
      if (t.state !== prevState) { prevState = t.state; onState(false); }
      else renderMeta();
      renderKpi();
    }
    // 推送、轮询、操作后都会触发刷新：同一时间只跑一次，期间又有变化则结束后再刷一次，避免事件重复显示
    let inflight = null, again = false;
    function refresh() {
      if (inflight) { again = true; return inflight; }
      inflight = (async () => { do { again = false; await doRefresh(); } while (again && scope.alive); })()
        .finally(() => { inflight = null; });
      return inflight;
    }
    // 实时推送：任务有变化（状态 / 事件）立即刷新，节点进度与模型输出直接渲染；连不上时退回轮询
    let sse = false;
    const kick = () => refresh().catch((e) => console.warn(e));
    if (window.EventSource) {
      const es = new EventSource(`/api/tasks/${encodeURIComponent(id)}/stream`);
      scope.on(() => es.close());
      es.addEventListener("open", () => { sse = true; $("#dlconn", root).textContent = "实时推送"; });
      es.addEventListener("error", () => { sse = false; $("#dlconn", root).textContent = "连接中断，改为定时刷新"; });
      es.addEventListener("live", (m) => { const r = JSON.parse(m.data); live[r.key] = r.data; renderLive(); });
      es.addEventListener("tick", () => kick());
      es.addEventListener("end", () => { es.close(); sse = false; kick(); });
    }
    scope.poll(refresh, () => (sse ? 5000 : AUTO.includes(t.state) ? 900 : 3000));
  }

  /* ================================================================ 04 审核台 */
  async function pageReviewList(scope) {
    const tasks = (S.tasks = await API.get("/api/tasks"));
    const list = tasks.filter((t) => t.state === "PENDING_REVIEW").concat(tasks.filter((t) => t.state === "DELIVERED"));
    scope.root.innerHTML = pageHead("04", `审核<span class="hl">台</span>`, "审核人在这里看周报、质检记录和与上一版的差异，然后通过或退回。退回意见会被拆成待办，重做后逐条校验。") +
      `<div class="task-grid">${list.map(taskCard).join("") || `<div class="empty" style="grid-column:1/-1"><div class="big">没有待审核的周报</div><p>质检全部通过的任务会出现在这里。</p></div>`}</div>`;
    $$(".task-card", scope.root).forEach((a) => (a.href = a.getAttribute("href").replace("#/tasks/", "#/review/")));
    if (list.some((t) => t.state === "PENDING_REVIEW")) FX.mood("review");
  }

  const NUM_RE = /([-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?:\s?元)?|[-+]?\d+(?:\.\d+)?%|[-+]?\d+(?:\.\d+)?\s?(?:元|个百分点))/g;
  const hlNum = (s) => esc(s).replace(NUM_RE, '<mark class="n">$1</mark>');
  function renderPaper(blocks, delivered, version) {
    let html = `<div class="stamp ${delivered ? "static" : ""}" id="stamp">已交付<small>APPROVED · V${version}</small></div>`;
    let inSec = false, inList = false;
    const endList = () => { if (inList) { html += "</ul>"; inList = false; } };
    const endSec = () => { endList(); if (inSec) { html += "</div>"; inSec = false; } };
    blocks.forEach((b) => {
      const v = b.value;
      if (b.kind !== "bullet") endList();
      switch (b.kind) {
        case "title": html += `<h1>${esc(v)}</h1>`; break;
        case "meta": html += `<div class="p-meta">${esc(String(v).replace(/　\|　生成时间：\s*$/, ""))}</div>`; break;
        case "h2": endSec(); html += `<div class="p-sec" data-in><h2>${esc(v)}</h2>`; inSec = true; break;
        case "p": html += `<p>${hlNum(v)}</p>`; break;
        case "bullet": if (!inList) { html += "<ul>"; inList = true; } html += `<li>${hlNum(v)}</li>`; break;
        case "anomaly":
          html += `<div class="anomaly ${/环比\s*-/.test(v.head) ? "" : "up"}"><div class="ah">${hlNum(v.head)}</div>
            ${[["现象", v.phenomenon], ["原因", v.cause], ["建议", v.suggestion]].filter(([, x]) => x).map(([k, x]) => `<div class="ar"><b>${k}</b>${hlNum(x)}</div>`).join("")}
            ${(v.checks || []).map((c) => `<div class="chk"><b>核对</b> ${hlNum(c)}</div>`).join("")}</div>`;
          break;
        case "table":
          html += `<div style="overflow-x:auto"><table><caption>${esc(v.title)}</caption><thead><tr>${v.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${v.rows.map((r) => `<tr>${r.map((c) => { const s = String(c ?? ""); return `<td class="${/^-\d.*%$/.test(s) ? "neg" : /^\+\d.*%$/.test(s) ? "pos" : ""}">${esc(s)}</td>`; }).join("")}</tr>`).join("")}</tbody></table></div>`;
          break;
        case "image": html += `<div class="imgs"><img src="${esc(v.url)}" alt="${esc(v.caption)}" loading="lazy"></div>`; break;
        case "image_row": html += `<div class="imgs">${v.map((x) => `<img src="${esc(x.url)}" alt="${esc(x.caption)}" loading="lazy">`).join("")}</div>`; break;
        case "footer": endSec(); html += `<div class="p-foot">${esc(v)}</div>`; break;
        default: break;
      }
    });
    endSec();
    return html;
  }
  const checkIco = (ok, i) => `<svg class="check-ico ${ok ? "ok" : "bad"}" viewBox="0 0 20 20" style="--i:${i}"><circle cx="10" cy="10" r="8.5"/><path d="${ok ? "M6 10.5l2.6 2.6L14 7.5" : "M7 7l6 6M13 7l-6 6"}"/></svg>`;

  async function pageReview(scope, id) {
    let r;
    try { r = await API.get(`/api/tasks/${id}/review`); }
    catch (e) {
      scope.root.innerHTML = `<div class="empty"><div class="big">这份任务还没有周报</div><p>${esc(e.message)}</p><a class="btn btn-ghost" href="#/tasks/${esc(id)}">${roll("查看任务")}</a></div>`;
      return;
    }
    const t = r.task;
    const delivered = t.state === "DELIVERED";
    const pending = t.state === "PENDING_REVIEW";
    FX.mood(delivered ? "success" : "review");
    scope.root.innerHTML = `
      <a class="link-arrow" href="#/review">← 审核台</a>
      <div class="page-head" style="margin-top:16px"><div><div class="eyebrow" data-reveal><b>04</b> / Review · <span class="mono">${esc(t.id)}</span></div>
        <h1 class="page-title" data-split>${esc(t.period)} 周报</h1></div>
        <div class="detail-meta" data-reveal>${stateChip(t.state, t.label, true)}<span class="chip mono">${esc(r.tag)}</span></div></div>
      <div class="review-layout">
        <div class="paper-stage"><article class="paper" id="paper">${renderPaper(r.blocks, delivered, t.version)}</article></div>
        <aside class="side">
          <div class="card" data-reveal style="--d:150ms">
            <div class="card-title"><span class="dot"></span>交付文件</div>
            <div class="row-between"><div><div style="font-weight:700">Word 周报 · ${esc(r.tag)}</div><div class="sha">sha256 ${esc(String(r.sha256).slice(0, 24))}…</div></div>
            <a class="icon-btn" ${r.primary_url ? `href="${esc(r.primary_url)}&download=1"` : `href="#" data-nodl`} data-cursor="下载" aria-label="下载 Word">${I.download}</a></div>
          </div>
          <div class="card" data-reveal style="--d:220ms">
            <div class="tabs" id="rtabs"><button data-t="qc" class="on">质检记录</button><button data-t="diff">与上一版差异</button><button data-t="fb">审核意见采纳</button><i class="ind"></i></div>
            <div id="rtab"></div>
          </div>
          <div id="rdec"></div>
        </aside>
      </div>`;
    $$("[data-nodl]", scope.root).forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); FX.toast("演示数据没有实际文件，连接后端后可下载", "info"); }));
    const tabs = $("#rtabs", scope.root), body = $("#rtab", scope.root);
    const placeInd = () => { const on = tabs.querySelector("button.on"), ind = tabs.querySelector(".ind"); if (on) { ind.style.left = on.offsetLeft + "px"; ind.style.width = on.offsetWidth + "px"; } };
    const showTab = (k) => {
      tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.t === k));
      placeInd();
      if (k === "qc") {
        const rounds = (r.qc && r.qc.rounds) || [];
        body.innerHTML = rounds.length ? rounds.map((rd) => `<div class="qc-round"><div class="qr-h"><b>第 ${rd.round_no} 次 · V${rd.version}</b><span class="chip ${rd.passed ? "up" : "down"}">${rd.passed ? "通过" : "未通过"}</span></div>
          <ul class="qc-list">${rd.results.map((x, i) => `<li class="qc-item" style="--i:${i}">${checkIco(x.passed, i)}<span>${esc(x.name)} <span class="sev">${esc(x.rule)} · ${esc(x.severity)}</span></span><span></span>${(() => { const ds = (x.details || []).filter((d) => !x.passed || (d && d.type === "note")); return ds.length ? `<div class="det">${ds.slice(0, 3).map((d) => esc(d.msg || d)).join("；")}</div>` : ""; })()}</li>`).join("")}</ul>
          ${rd.fix_actions && rd.fix_actions.length ? `<div class="fix-actions">修正动作：${rd.fix_actions.map(esc).join("；")}</div>` : ""}</div>`).join("") : `<p class="muted">暂无质检记录</p>`;
      } else if (k === "diff") {
        body.innerHTML = r.diff ? `<div class="diff">${r.diff.split("\n").map((l) => `<span class="${/^\+(?!\+\+)/.test(l) ? "add" : /^-(?!--)/.test(l) ? "del" : /^@@/.test(l) ? "hunk" : ""}">${esc(l) || " "}</span>`).join("")}</div>` : `<p class="muted">这是第一版，没有可比较的上一版。</p>`;
      } else {
        const f = r.feedback;
        body.innerHTML = f ? `<p style="margin-top:0"><span class="faint">审核意见：</span>${esc(f.comments)}</p><div class="todo">${(f.todos || []).map((x) => `<div><span class="chip mono">${esc(x.section || "")}</span> ${esc(x.instruction || JSON.stringify(x))}<div class="chips" style="margin-top:6px">${(x.keywords || []).map((k2) => `<span class="chip tool">${esc(k2)}</span>`).join("")}</div></div>`).join("")}</div>
          <p class="faint" style="font-size:12px;margin-bottom:0">重做后由质检规则 feedback_addressed 逐条校验，不通过不能再次送审。</p>` : `<p class="muted">本版本没有待落实的审核意见。</p>`;
      }
    };
    tabs.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.t)));
    showTab("qc");
    setTimeout(placeInd, 400);

    const renderDecision = () => {
      const dec = $("#rdec", scope.root);
      if (delivered) {
        dec.innerHTML = `<div class="alert ok"><span class="a-ico">✓</span><h4>已交付</h4><p>审核版本与交付文件哈希一致，质检记录随交付包保存。</p></div>`;
        return;
      }
      if (!pending) { dec.innerHTML = `<div class="alert warn"><span class="a-ico">!</span><h4>当前不可审核</h4><p>任务状态为「${esc(t.label)}」。</p></div>`; return; }
      dec.innerHTML = `<div class="card decision">
        <div class="card-title"><span class="dot" style="background:var(--violet)"></span>审核决定</div>
        ${S.role !== "reviewer" ? `<p class="muted" style="font-size:13px;margin-top:0">当前身份是<b style="color:var(--amber)">发起人</b>。通过 / 退回只能由审核人操作——程序会校验触发方，越权操作被拒绝并留痕。可在右上角切换身份。</p>` : ""}
        <textarea class="textarea" id="rcom" placeholder="退回意见（退回时必填，可写多条，用分号或换行分隔）"></textarea>
        <div class="chips" style="margin-top:10px">${["华东区下滑原因没说清楚，请结合周会纪要补充", "摘要请补充目标达成率", "下周关注事项请写明负责人"].map((s) => `<button class="chip chip-btn" data-s="${esc(s)}">${esc(s)}</button>`).join("")}</div>
        <div class="btns"><button class="btn btn-primary" id="rok">${roll("通过并交付")}</button><button class="btn btn-ghost" id="rno">${roll("退回修改")}</button></div>
        <div id="rcheck"></div>
        <div class="sha" style="margin-top:12px">通过时提交审核版本哈希 ${esc(String(r.sha256).slice(0, 12))}…，程序比对一致才允许交付</div></div>`;
      $$("[data-s]", dec).forEach((b) => b.addEventListener("click", () => { const ta = $("#rcom", dec); ta.value = ta.value ? ta.value + "；" + b.dataset.s : b.dataset.s; ta.focus(); }));
      $("#rok", dec).addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        const [x, y] = centerOf(btn);
        busy(btn, true);
        try {
          await API.post(`/api/tasks/${id}/review`, { decision: "approve", sha256: r.sha256, role: S.role });
          $("#stamp", scope.root).classList.add("on");
          FX.burst(x, y, { n: 200, power: 17 });
          FX.mood("success"); FX.pulse(1);
          FX.toast("审核通过，已生成交付包", "ok");
          scope.later(() => navigate(), 1800);
        } catch (err) {
          busy(btn, false);
          FX.toast(err.message, "err", 7000);
          FX.shock(x, y);
          const p = $("#paper", scope.root);
          p.classList.remove("shake"); void p.offsetWidth; p.classList.add("shake");
        }
      });
      const sendBack = async (btn, confirm) => {
        const [x, y] = centerOf(btn);
        busy(btn, true);
        try {
          await API.post(`/api/tasks/${id}/review`, { decision: "changes", comments: $("#rcom", dec).value, role: S.role, confirm });
          $("#paper", scope.root).classList.add("away");
          FX.toast("已退回，员工正在按意见重做", "ok");
          scope.later(() => { location.hash = `#/tasks/${id}`; }, 750);
        } catch (err) {
          busy(btn, false);
          if (err.data && err.data.kind === "feedback_check") {
            feedbackCheckBox($("#rcheck", dec), err.data.issues || [], (b) => sendBack(b, true));
            return;
          }
          FX.toast(err.message, "err", 7000);
          FX.shock(x, y);
          const p = $("#paper", scope.root);
          p.classList.remove("shake"); void p.offsetWidth; p.classList.add("shake");
        }
      };
      $("#rno", dec).addEventListener("click", (e) => sendBack(e.currentTarget, false));
      $("#rcom", dec).addEventListener("input", () => { $("#rcheck", dec).innerHTML = ""; });
    };
    renderDecision();
    scope.listen(window, "rolechange", renderDecision);
    FX.bind(scope.root);
  }

  /* ================================================================ 05 事件回放 */
  const EV_GROUPS = {
    state: ["状态", ["state_changed", "transition_rejected", "task_created", "task_resumed"]],
    step: ["步骤", ["step_started", "step_completed", "step_skipped", "admission_checked", "form_validated"]],
    tool: ["工具", ["tool_called", "tool_failed", "tool_denied"]],
    model: ["模型", ["model_called", "model_output_invalid"]],
    qc: ["质检", ["qc_run"]],
    review: ["审核交付", ["review_submitted", "feedback_parsed", "delivered"]],
  };
  async function pageEvents(scope, id) {
    const tasks = (S.tasks = await API.get("/api/tasks"));
    if (!id && tasks.length) { history.replaceState(null, "", `#/events/${tasks[0].id}`); id = tasks[0].id; }
    if (!id) {
      scope.root.innerHTML = pageHead("05", `事件<span class="hl">回放</span>`, "") + `<div class="empty"><div class="big">还没有事件</div><p>发起一个任务后，这里可以回放它的完整执行过程。</p></div>`;
      return;
    }
    const evs = await API.get(`/api/tasks/${id}/events`);
    const t = tasks.find((x) => x.id === id) || {};
    scope.root.innerHTML = pageHead("05", `事件<span class="hl">回放</span>`, "状态变化、模型调用、工具调用、质检、审核全部只追加写入 SQLite。程序被中断后据此从断点继续，也能在这里逐条回放。",
      `<select class="select" id="esel">${tasks.map((x) => `<option value="${esc(x.id)}" ${x.id === id ? "selected" : ""}>${esc(x.id)} · ${esc(x.period)} · ${esc(x.label)}</option>`).join("")}</select>`) + `
      <div class="replay-layout">
        <div class="stack">
          <div class="card graph-compact" data-reveal><div class="row-between"><div class="card-title"><span class="dot"></span>状态轨迹</div>${stateChip(t.state, t.label)}</div><div id="egraph"></div></div>
          <div class="counters" data-reveal>${[["model", "模型调用"], ["tool", "工具调用"], ["fail", "工具失败"], ["qc", "质检轮次"]].map(([k, l]) => `<div><b data-k="${k}">0</b><span>${l}</span></div>`).join("")}</div>
          <div class="card" data-reveal><div class="card-title"><span class="dot"></span>当前进度</div><div id="enow" class="muted">—</div></div>
        </div>
        <div>
          <div class="player" data-reveal>
            <button class="play" id="eplay" aria-label="播放" data-cursor="回放">${I.play}</button>
            <button class="icon-btn" id="erestart" aria-label="从头开始">${I.restart}</button>
            <input type="range" class="scrub" id="escrub" min="0" max="${evs.length}" value="${evs.length}">
            <div class="seg" id="espeed">${[1, 4, 16].map((s) => `<button data-s="${s}" class="${s === 4 ? "on" : ""}">${s}×</button>`).join("")}</div>
          </div>
          <div class="filters" id="efil">${Object.entries(EV_GROUPS).map(([k, [lab]]) => `<button class="chip chip-btn on" data-g="${k}">${lab}</button>`).join("")}</div>
          <div class="console" data-reveal><div class="console-top"><i></i><i></i><i></i><span>events · task_id = ${esc(id)}</span></div><div class="feed" id="efeed"></div></div>
        </div>
      </div>`;
    $("#esel", scope.root).addEventListener("change", (e) => { location.hash = `#/events/${e.target.value}`; });
    const graph = Graph.create($("#egraph", scope.root), S.meta);
    const feed = $("#efeed", scope.root), scrub = $("#escrub", scope.root), playBtn = $("#eplay", scope.root), now = $("#enow", scope.root);
    const on = new Set(Object.keys(EV_GROUPS));
    const groupOf = (type) => (Object.entries(EV_GROUPS).find(([, [, ts]]) => ts.includes(type)) || ["other"])[0];
    let idx = evs.length, playing = false, speed = 4, timer = 0;
    const counters = () => {
      const upto = evs.slice(0, idx);
      const c = { model: upto.filter((e) => e.type === "model_called").length, tool: upto.filter((e) => e.type === "tool_called").length, fail: upto.filter((e) => e.type === "tool_failed").length, qc: upto.filter((e) => e.type === "qc_run").length };
      $$(".counters b", scope.root).forEach((b) => {
        const v = c[b.dataset.k];
        if (b.textContent !== String(v)) { b.textContent = v; if (playing) { b.classList.remove("bump"); void b.offsetWidth; b.classList.add("bump"); } }
      });
    };
    const visible = (e) => on.has(groupOf(e.type)) || groupOf(e.type) === "other";
    const rebuild = () => {
      feed.innerHTML = "";
      evs.slice(0, idx).filter(visible).forEach((e) => { const r = feedRow(e); r.style.animation = "none"; feed.appendChild(r); });
      const caret = document.createElement("div");
      caret.innerHTML = `<span class="caret"></span>`;
      feed.appendChild(caret);
      feed.scrollTop = feed.scrollHeight;
      const path = Graph.pathFromEvents(evs.slice(0, idx));
      graph.setPath(path, path.length ? path[path.length - 1][1] : "CREATED");
      counters();
      scrub.value = idx;
      scrub.style.setProperty("--p", `${(idx / Math.max(1, evs.length)) * 100}%`);
      const lastStep = [...evs.slice(0, idx)].reverse().find((e) => e.type === "step_started");
      now.innerHTML = idx ? `第 ${idx}/${evs.length} 条 · ${esc(time(evs[idx - 1].ts))}${lastStep ? ` · 最近步骤「${esc(lastStep.payload.name || lastStep.payload.key)}」` : ""}` : "从头开始";
    };
    const step = () => {
      if (!scope.alive) return;
      if (idx >= evs.length) { playing = false; playBtn.innerHTML = I.play; return; }
      const e = evs[idx++];
      if (visible(e)) {
        const r = feedRow(e);
        feed.insertBefore(r, feed.lastChild);
        feed.scrollTop = feed.scrollHeight;
      }
      if (e.type === "state_changed") {
        graph.comet(e.payload.from, e.payload.to, Math.max(260, 900 / Math.sqrt(speed)));
        const path = Graph.pathFromEvents(evs.slice(0, idx));
        scope.later(() => graph.setPath(path, e.payload.to), Math.max(240, 860 / Math.sqrt(speed)));
        if (e.payload.to === "DELIVERED") { const [x, y] = centerOf($("#egraph", scope.root)); FX.burst(x, y, { n: 120 }); }
        if (e.payload.to === "SUSPENDED") FX.glitch();
        FX.mood(MOOD_OF[e.payload.to] || "idle");
      }
      counters();
      scrub.value = idx;
      scrub.style.setProperty("--p", `${(idx / Math.max(1, evs.length)) * 100}%`);
      const lastStep = [...evs.slice(0, idx)].reverse().find((x) => x.type === "step_started");
      now.innerHTML = `第 ${idx}/${evs.length} 条 · ${esc(time(e.ts))}${lastStep ? ` · 最近步骤「${esc(lastStep.payload.name || lastStep.payload.key)}」` : ""}`;
      const next = evs[idx];
      const dt = next ? Math.min(1400, Math.max(80, Date.parse(next.ts) - Date.parse(e.ts) || 200)) : 0;
      timer = setTimeout(step, dt / speed + (e.type === "state_changed" ? 260 : 40));
    };
    const play = () => {
      if (playing) { playing = false; clearTimeout(timer); playBtn.innerHTML = I.play; return; }
      if (idx >= evs.length) { idx = 0; rebuild(); }
      playing = true; playBtn.innerHTML = I.pause;
      step();
    };
    scope.on(() => clearTimeout(timer));
    playBtn.addEventListener("click", play);
    $("#erestart", scope.root).addEventListener("click", () => { clearTimeout(timer); playing = false; idx = 0; rebuild(); play(); });
    scrub.addEventListener("input", () => { clearTimeout(timer); playing = false; playBtn.innerHTML = I.play; idx = +scrub.value; rebuild(); });
    $$("#espeed button", scope.root).forEach((b) => b.addEventListener("click", () => { speed = +b.dataset.s; $$("#espeed button", scope.root).forEach((x) => x.classList.toggle("on", x === b)); }));
    $$("#efil button", scope.root).forEach((b) => b.addEventListener("click", () => { const g = b.dataset.g; on.has(g) ? on.delete(g) : on.add(g); b.classList.toggle("on", on.has(g)); rebuild(); }));
    rebuild();
  }

  /* ================================================================ 06 故障注入 */
  const FAULT_DESC = {
    "search_minutes:timeout": "异常原因分析时检索周会纪要超时。网关按 1/2/4 秒退避重试 3 次，仍失败就暂停，并说明第几步、什么原因、已完成几步。",
    "read_minutes:timeout": "准入阶段读取纪要的服务不可用。任务在准入校验处暂停，服务恢复后从断点继续。",
    "query_kpi:error": "模型查询指标时返回 503。失败如实上报，模型拿不到数字也不能自己编。",
    "llm:timeout": "模型接口断开（模拟断网）。重试耗尽后暂停转人工，不崩溃；恢复后从失败的步骤继续，已完成步骤不重跑。",
  };
  async function pageFaults(scope) {
    const opts = (S.meta.faults || []).filter((f) => f.scope !== "task");
    let cur = (S.faults = await API.get("/api/faults"));
    const isOn = (f) => cur.some((x) => x.tool === f.tool && x.mode === f.mode);
    scope.root.innerHTML = pageHead("06", `故障<span class="hl">注入</span>`, "对之后的所有工具调用生效，用于现场演示“工具故障的处理过程”。也可以在发起任务时只对单个任务注入。") + adminBar("开关全局故障") + `
      <div class="grid g2">${opts.map((f, i) => `<div class="card spot fault-card ${isOn(f) ? "on" : ""}" data-reveal style="--d:${i * 80}ms" data-id="${esc(f.id)}">
        <div class="fc-top"><div><div class="tool">${esc(f.tool)} · ${esc(f.mode)}</div><h3>${esc(f.label)}</h3></div>
        <label class="toggle danger"><input type="checkbox" ${isOn(f) ? "checked" : ""}><span class="sw"></span></label></div>
        <p>${esc(FAULT_DESC[f.id] || "")}</p></div>`).join("")}</div>
      <section class="section" style="margin-top:70px">
        <div class="section-head"><div><div class="eyebrow" data-reveal>tools/gateway.py</div><h2 class="section-title" data-split>故障时，员工会这样做</h2></div></div>
        <div class="flowline" data-reveal>${[["bad", "工具调用失败", "超时 / 5xx / 断网"], ["mid", "退避重试", "1s → 2s → 4s，共 3 次"], ["bad", "仍然失败", "不编造结果，不崩溃"], ["mid", "暂停转人工", "说明第几步、原因、已完成 x/y"], ["", "服务恢复", "关闭故障 / 网络恢复"], ["ok", "从断点继续", "已完成步骤跳过，不重跑"]]
          .map(([c, b, s], i) => `<div class="${c} pulse" style="--i:${i}"><b>${b}</b><span class="mono">${s}</span></div>`).join("")}</div>
      </section>
      <section class="section" style="margin-top:70px">
        <div class="grid split-7-5">
          <div class="card" data-reveal><div class="card-title"><span class="dot" style="background:var(--amber)"></span>现场演示脚本（约 1 分钟）</div>
            <ol style="margin:0;padding-left:20px;display:grid;gap:10px;color:var(--ink-2)">
              <li>打开上面的「模型接口断开」或「检索服务超时」</li><li>点右侧按钮，用官方样例发起一个第 44 周任务</li>
              <li>看任务自动重试 3 次后暂停，提示停在第几步、已完成几步</li><li>回到这里关闭故障（模拟恢复），在任务页点「从断点重试」</li>
              <li>在事件回放里看到已完成的步骤被跳过，任务继续到待审核</li></ol></div>
          <div class="card spot" data-reveal style="--d:120ms;display:flex;flex-direction:column;justify-content:space-between;gap:18px">
            <div><div class="card-title"><span class="dot"></span>一键开始</div><p class="muted" style="margin:0">用官方样例数据（销售明细.xlsx + 第 44 周纪要）发起任务，当前开启的全局故障会生效。</p></div>
            <button class="btn btn-primary" id="fgo" data-magnetic>${roll("发起演示任务")}<span class="arrow">${I.arrow}</span></button></div>
        </div>
      </section>`;
    bindAdminBar(scope.root);
    const sync = () => { FX.mood(cur.length ? "danger" : "idle"); S.faults = cur; renderHazard(); };
    sync();
    $$(".fault-card", scope.root).forEach((card) => {
      const cb = card.querySelector("input");
      cb.addEventListener("change", async () => {
        const ids = $$(".fault-card", scope.root).filter((c) => c.querySelector("input").checked).map((c) => c.dataset.id);
        try {
          cur = await API.post("/api/faults", { ids });
          card.classList.toggle("on", cb.checked);
          if (cb.checked) { FX.glitch(); FX.toast(`已开启：${card.querySelector("h3").textContent}`, "warn"); }
          else FX.toast(`已关闭：${card.querySelector("h3").textContent}（模拟服务恢复）`, "ok");
          sync();
        } catch (e) { cb.checked = !cb.checked; FX.toast(e.message, "err"); }
      });
    });
    $("#fgo", scope.root).addEventListener("click", async (e) => {
      const r = await act(e.currentTarget, () => API.post("/api/tasks", { values: { period: "2026-W44" }, use_sample: true, faults: [] }));
      if (r && r.id) { FX.toast(`已发起演示任务 ${r.id}`, "ok"); location.hash = `#/tasks/${r.id}`; }
    });
  }

  /* ================================================================ 07 员工配置 */
  function hlYaml(src) {
    const e = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    return src.split("\n").map((line) => {
      let q = null, ci = -1;
      for (let i = 0; i < line.length; i++) {
        const ch = line[i];
        if (q) { if (ch === q) q = null; }
        else if (ch === '"' || ch === "'") q = ch;
        else if (ch === "#" && (i === 0 || /\s/.test(line[i - 1]))) { ci = i; break; }
      }
      const code = ci >= 0 ? line.slice(0, ci) : line, com = ci >= 0 ? line.slice(ci) : "";
      let out = "", rest = code;
      const m = code.match(/^(\s*(?:-\s+)?)([^:{}[\],"'#]+?)(:)(?=\s|$)/);
      if (m) { out += e(m[1]) + `<span class="hl-k">${e(m[2])}</span>:`; rest = code.slice(m[0].length); }
      const re = /("[^"]*"|'[^']*')|(\b\d+(?:\.\d+)?\b)|([\w一-鿿]+)(?=:\s)|\b(true|false|null)\b/g;
      let last = 0, mm;
      while ((mm = re.exec(rest))) {
        out += e(rest.slice(last, mm.index));
        if (mm[1]) out += `<span class="hl-s">${e(mm[1])}</span>`;
        else if (mm[2]) out += `<span class="hl-n">${e(mm[2])}</span>`;
        else if (mm[3]) out += `<span class="hl-k">${e(mm[3])}</span>`;
        else out += `<span class="hl-p">${e(mm[4])}</span>`;
        last = re.lastIndex;
      }
      out += e(rest.slice(last));
      return out + (com ? `<span class="hl-c">${e(com)}</span>` : "");
    }).join("\n");
  }
  function hlMd(src) {
    const e = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    let fm = false;
    return src.split("\n").map((line, i) => {
      if (line.trim() === "---" && i === 0) { fm = true; return `<span class="hl-c">${e(line)}</span>`; }
      if (fm && line.trim() === "---") { fm = false; return `<span class="hl-c">${e(line)}</span>`; }
      if (fm) return `<span class="hl-c">${e(line)}</span>`;
      if (/^#{1,6}\s/.test(line)) return `<span class="hl-h">${e(line)}</span>`;
      let s = e(line);
      s = s.replace(/\{\{[^}]*\}\}/g, (x) => `<span class="hl-n">${x}</span>`);
      s = s.replace(/`([^`]+)`/g, (x) => `<span class="hl-s">${x}</span>`);
      s = s.replace(/\*\*([^*]+)\*\*/g, (x) => `<span class="hl-b">${x}</span>`);
      s = s.replace(/^(\s*)([-*]|\d+\.)(\s)/, (x, a, b, c) => `${a}<span class="hl-k">${b}</span>${c}`);
      return s;
    }).join("\n");
  }
  function editor(host, text, kind) {
    host.innerHTML = `<div class="editor"><div class="gutter"></div><pre aria-hidden="true"></pre><textarea spellcheck="false" wrap="off"></textarea></div>`;
    const ta = host.querySelector("textarea"), pre = host.querySelector("pre"), gut = host.querySelector(".gutter");
    ta.value = text;
    const hl = kind === "yaml" ? hlYaml : hlMd;
    const paint = () => {
      pre.innerHTML = hl(ta.value) + "\n ";
      const n = ta.value.split("\n").length;
      if (gut.children.length !== n) gut.innerHTML = Array.from({ length: n }, (_, i) => `<div>${i + 1}</div>`).join("");
    };
    const sync = () => { pre.scrollTop = ta.scrollTop; pre.scrollLeft = ta.scrollLeft; gut.scrollTop = ta.scrollTop; };
    ta.addEventListener("input", () => { paint(); sync(); });
    ta.addEventListener("scroll", sync);
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Tab") {
        e.preventDefault();
        const s = ta.selectionStart;
        ta.setRangeText("  ", s, ta.selectionEnd, "end");
        paint();
      }
    });
    paint();
    return { get value() { return ta.value; }, set value(v) { ta.value = v; paint(); }, ta };
  }
  async function pageConfig(scope) {
    const cfg = await API.get("/api/config");
    const emp = S.meta.employee;
    const tabsDef = [["yaml", "employee.yaml · 结构"], ["skill", "SKILL.md · 做法"], ["flow", "状态流转表"], ["tools", "工具与上限"]];
    scope.root.innerHTML = pageHead("07", `员工<span class="hl">配置</span>`, "员工的岗位职责、工作步骤、开放工具、质检规则和上限都写在配置文件里。业务人员改这里即可调整员工做法，保存前自动校验，有错误不会写入。") +
      adminBar("保存配置") + (S.meta.config_error ? `<div class="alert danger" style="margin-bottom:18px"><span class="a-ico">!</span><h4>员工配置有误，其他页面暂不可用</h4><p>${esc(S.meta.config_error.join("\n"))}</p></div>` : "") + `
      <div class="tabs" id="ctabs">${tabsDef.map(([k, l], i) => `<button data-t="${k}" class="${i ? "" : "on"}">${l}</button>`).join("")}<i class="ind"></i></div>
      <div id="cbody"></div>`;
    bindAdminBar(scope.root);
    const tabs = $("#ctabs", scope.root), body = $("#cbody", scope.root);
    const texts = { yaml: cfg["employee.yaml"], skill: cfg["SKILL.md"] };
    const placeInd = () => { const on = tabs.querySelector("button.on"), ind = tabs.querySelector(".ind"); if (on) { ind.style.left = on.offsetLeft + "px"; ind.style.width = on.offsetWidth + "px"; } };
    let ed = null, edKind = null;
    const show = (k) => {
      if (ed && edKind) texts[edKind] = ed.value;
      ed = null; edKind = null;
      tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.t === k));
      placeInd();
      if (k === "yaml" || k === "skill") {
        const name = k === "yaml" ? "employee.yaml" : "SKILL.md";
        body.innerHTML = `<div id="edhost"></div><div class="cfg-actions">
          <button class="btn btn-ghost btn-sm" id="cval">${roll("只校验")}</button>
          <button class="btn btn-primary btn-sm" id="csave">${roll("校验并保存")}</button>
          <button class="btn btn-sm btn-ghost" id="creset">${roll("还原")}</button>
          <div class="input-line" style="flex:1;min-width:180px"><input type="text" id="cnote" placeholder="修改说明（记入修改记录）"></div>
          <span class="faint" style="font-size:12px"><span class="kbd">Ctrl</span> + <span class="kbd">S</span> 保存 · 保存后对新执行的步骤生效</span></div><ul class="errs" id="cerr"></ul>`;
        ed = editor($("#edhost", body), texts[k], k === "yaml" ? "yaml" : "md");
        edKind = k;
        const run = async (save, btn) => {
          const res = await act(btn, () => API.post("/api/config", { name, text: ed.value, save, note: $("#cnote", body).value }));
          if (!res) return;
          const list = $("#cerr", body);
          const items = res.errors.map((x) => ["e", x]).concat(res.warnings.map((x) => ["w", x]));
          if (!res.errors.length) items.unshift(["ok", save ? `已保存 ${name}，校验通过${res.seq ? `（修改记录 #${res.seq}）` : ""}` : "校验通过，可以保存"]);
          list.innerHTML = items.map(([c, x], i) => `<li class="${c}" style="--i:${i}">${c === "e" ? "✗ " : c === "w" ? "! " : "✓ "}${esc(x)}</li>`).join("");
          if (res.errors.length) { FX.toast(`配置有 ${res.errors.length} 处错误，未保存`, "err"); FX.glitch(); }
          else if (save) { FX.toast(`已保存 ${name}`, "ok"); texts[k] = ed.value; cfg[name] = ed.value; const [x, y] = centerOf(btn); FX.burst(x, y, { n: 70, power: 10 }); }
        };
        $("#cval", body).addEventListener("click", (e) => run(false, e.currentTarget));
        $("#csave", body).addEventListener("click", (e) => run(true, e.currentTarget));
        $("#creset", body).addEventListener("click", () => { ed.value = cfg[name]; $("#cerr", body).innerHTML = ""; FX.toast("已还原为当前保存的版本", "info", 2000); });
        ed.ta.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); run(true, $("#csave", body)); } });
      } else if (k === "flow") {
        const tr = S.meta.transitions;
        const seen = new Set();
        const rows = tr.filter((x) => { if (x.event !== "cancel") return true; if (seen.has("c")) return false; seen.add("c"); return true; });
        body.innerHTML = `<div class="card" style="margin-bottom:18px"><div id="cgraph"></div></div>
          <div class="card table-scroll"><table class="tt"><thead><tr><th>当前状态</th><th>事件</th><th>闸门条件（程序判定）</th><th>目标状态</th><th>触发方</th></tr></thead>
          <tbody>${rows.map((x) => x.event === "cancel" ? `<tr><td>任一非终态</td><td class="ev">cancel</td><td>—</td><td>${stateChip("CANCELLED", L("CANCELLED"))}</td><td>发起人</td></tr>` :
            `<tr><td>${stateChip(x.src, L(x.src))}</td><td class="ev">${esc(x.event)}</td><td>${esc(x.desc)}${x.guard ? ' <span class="chip tool" style="margin-left:4px">闸门</span>' : ""}</td><td>${x.dst ? stateChip(x.dst, L(x.dst)) : "暂停前的状态（断点）"}</td><td>${x.actors.map((a) => ACTOR[a] || a).join(" / ")}</td></tr>`).join("")}</tbody></table></div>`;
        const g = Graph.create($("#cgraph", body), S.meta);
        g.setPath([], null);
      } else {
        const steps = (emp && emp.steps) || [];
        const lim = (emp && emp.limits) || {};
        const LIM = [["max_model_turns_per_step", "单步模型轮次"], ["max_step_seconds", "单步时长（秒）"], ["max_task_seconds", "任务总时长（秒）"], ["tool_max_retries", "工具重试次数"], ["tool_timeout_seconds", "工具超时（秒）"], ["qc_max_fix_rounds", "质检自动修正轮次"], ["rework_max_rounds", "返工次数上限"]];
        body.innerHTML = `<div class="grid split-7-5">
          <div class="card"><div class="card-title"><span class="dot"></span>工具 · 每步只开放指定的工具</div>
            <table class="tt"><thead><tr><th>步骤</th><th>执行方</th><th>可用工具</th></tr></thead><tbody>
            ${steps.map((s, i) => `<tr><td><span class="mono faint">0${i + 1}</span> ${esc(s.name)}</td><td>${s.type === "agent" ? '<span class="chip model">模型</span>' : '<span class="chip">程序</span>'}</td><td><div class="chips">${(s.tools || []).map((x) => `<span class="chip tool">${esc(x)}</span>`).join("")}</div></td></tr>`).join("")}
            </tbody></table><p class="faint" style="font-size:12px;margin-bottom:0">模型调用本步未开放的工具会被网关拒绝，并写入 tool_denied 事件。</p></div>
          <div class="stack"><div class="card"><div class="card-title"><span class="dot"></span>上限</div><div class="limits">${LIM.filter(([k2]) => lim[k2] != null).map(([k2, l]) => `<div><b>${esc(lim[k2])}</b><span>${l}</span></div>`).join("")}</div>
            <p class="faint" style="font-size:12px;margin-bottom:0">超限时任务暂停并转人工处理；取值理由见 README 与技术报告。</p></div>
          <div class="card"><div class="card-title"><span class="dot"></span>自动质检规则</div><div class="chips">${((emp && emp.quality_checks) || []).map((q) => `<span class="chip">${esc(q.name)} <span class="mono faint">${esc(q.rule)}</span></span>`).join("")}</div></div></div>
        </div>`;
      }
      FX.bind(body);
    };
    tabs.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => show(b.dataset.t)));
    show("yaml");
    setTimeout(placeInd, 300);
  }

  const PAGES = { home: pageHome, newTask: pageNew, tasks: pageTasks, task: pageTask, reviewList: pageReviewList, review: pageReview, events: pageEvents, faults: pageFaults, config: pageConfig };

  /* ------------------------------------------------------------ 开场加载 */
  async function loader(work) {
    const el = $("#loader");
    const num = $(".loader-count b", el), bar = $(".loader-bar i", el), lines = $(".loader-lines", el);
    let quick = false;
    try { quick = sessionStorage.getItem("me-seen") === "1"; sessionStorage.setItem("me-seen", "1"); } catch (e) { /* ignore */ }
    const msgs = ["加载员工配置 employee.yaml", "注册 MCP 工具", "载入状态流转表", "连接事件存储 tasks.db", "员工就绪"];
    const dur = quick || FX.reduce ? 450 : 1900;
    const t0 = performance.now();
    let done = false, result;
    work.then((r) => { done = true; result = r; }, (e) => { done = true; result = e; });
    await new Promise((resolve) => {
      const tick = (now) => {
        let p = Math.min(1, (now - t0) / dur);
        if (!done) p = Math.min(p, 0.9);
        const e = 1 - Math.pow(1 - p, 3);
        num.textContent = String(Math.round(e * 100)).padStart(3, "0");
        bar.style.transform = `scaleX(${e})`;
        lines.textContent = msgs[Math.min(msgs.length - 1, Math.floor(e * msgs.length))];
        if (p >= 1 && done) resolve();
        else requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
    el.classList.add("done");
    setTimeout(() => el.remove(), 1200);
    return result;
  }

  /* ------------------------------------------------------------ 启动 */
  async function boot() {
    FX.initBackground($("#bg"));
    FX.initGrain($(".grain"));
    FX.initCursor();
    const work = (async () => {
      S.mock = location.protocol === "file:" || /[?&]demo\b/.test(location.search);
      if (!S.mock) {
        try { const r = await fetch("/api/meta"); if (!r.ok) throw new Error(); S.meta = await r.json(); }
        catch (e) { S.mock = true; }
      }
      if (S.mock) S.meta = await MockAPI.request("GET", "/api/meta");
      S.tasks = await API.get("/api/tasks").catch(() => []);
      return true;
    })();
    const ok = await loader(work);
    if (ok !== true) {
      $("#view").innerHTML = `<div class="empty"><div class="big">无法启动</div><p>${esc((ok && ok.message) || ok)}</p></div>`;
      return;
    }
    if (S.mock) { const b = document.createElement("div"); b.className = "demo-banner"; b.textContent = "演示数据 · 未连接后端（运行 app/web_server.py 后显示真实任务）"; document.body.appendChild(b); }
    buildChrome();
    await pollGlobal();
    setInterval(pollGlobal, 3000);
    addEventListener("hashchange", navigate);
    navigate();
  }
  boot();
})();
