/* 状态机图：12 个状态、流转边、当前状态光晕、轨迹、彗星动画、悬停说明 */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const W = 132, H = 54;
  const POS = {
    WAITING_INPUT: [395, 140], AUTO_FIXING: [710, 140], REWORK: [920, 140],
    CREATED: [80, 310], ADMISSION: [290, 310], RUNNING: [500, 310], QC: [710, 310], PENDING_REVIEW: [920, 310], DELIVERED: [1130, 310],
    REJECTED: [185, 480], SUSPENDED: [710, 480], CANCELLED: [1130, 480],
  };
  const MAIN = new Set(["submit", "admission_pass", "steps_done", "qc_pass", "approve"]);
  const ACTOR = { system: "系统", initiator: "发起人", reviewer: "审核人" };
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const el = (tag, attrs = {}, parent) => {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  };
  const anchor = (s, side, off) => {
    const [x, y] = POS[s];
    if (side === "r") return [x + W / 2, y + off];
    if (side === "l") return [x - W / 2, y + off];
    if (side === "t") return [x + off, y - H / 2];
    return [x + off, y + H / 2];
  };
  const P = (a) => `${a[0].toFixed(1)},${a[1].toFixed(1)}`;

  // 个别会穿过其他节点的边，手工绕行
  const OVERRIDE = {
    "REWORK>WAITING_INPUT": () => { const a = anchor("REWORK", "t", 0), b = anchor("WAITING_INPUT", "t", 0); return `M${P(a)} C${a[0]},28 ${b[0]},28 ${P(b)}`; },
    "AUTO_FIXING>SUSPENDED": () => { const a = anchor("AUTO_FIXING", "r", 0), b = anchor("SUSPENDED", "r", -10); return `M${P(a)} C840,150 840,470 ${P(b)}`; },
    "REWORK>SUSPENDED": () => { const a = anchor("REWORK", "r", 0), b = anchor("SUSPENDED", "r", 12); return `M${P(a)} C1040,140 1030,230 1025,320 C1020,430 900,492 ${P(b)}`; },
  };

  function route(src, dst, k) {
    const key = `${src}>${dst}`;
    if (OVERRIDE[key]) return OVERRIDE[key]();
    const [x1, y1] = POS[src], [x2, y2] = POS[dst];
    const dx = x2 - x1, dy = y2 - y1;
    if (Math.abs(dy) < 1) {
      const s = dx > 0 ? ["r", "l"] : ["l", "r"];
      const a = anchor(src, s[0], k), b = anchor(dst, s[1], k);
      if (Math.abs(dx) > 260) { // 同一行但不相邻：从上方绕
        const a2 = anchor(src, "t", 0), b2 = anchor(dst, "t", 0);
        return `M${P(a2)} C${a2[0]},${a2[1] - 90} ${b2[0]},${b2[1] - 90} ${P(b2)}`;
      }
      return `M${P(a)} L${P(b)}`;
    }
    const s = dy > 0 ? ["b", "t"] : ["t", "b"];
    if (Math.abs(dx) < 1) {
      const a = anchor(src, s[0], k), b = anchor(dst, s[1], k);
      return `M${P(a)} L${P(b)}`;
    }
    const off = clamp(dx * 0.12, -50, 50);
    const a = anchor(src, s[0], off + k), b = anchor(dst, s[1], -off + k);
    const my = (a[1] + b[1]) / 2;
    return `M${P(a)} C${a[0].toFixed(1)},${my.toFixed(1)} ${b[0].toFixed(1)},${my.toFixed(1)} ${P(b)}`;
  }

  function create(container, meta, opts = {}) {
    const labels = meta.states;
    const trans = meta.transitions.filter((t) => t.event !== "cancel" && t.dst);
    // 同一对节点的边合并
    const groups = new Map();
    trans.forEach((t) => {
      const key = `${t.src}>${t.dst}`;
      if (!groups.has(key)) groups.set(key, { src: t.src, dst: t.dst, items: [] });
      groups.get(key).items.push(t);
    });
    const wrap = document.createElement("div");
    wrap.className = "graph-wrap";
    const svg = el("svg", { viewBox: "0 10 1220 530", role: "img", "aria-label": "任务状态流转图" });
    wrap.appendChild(svg);
    const tip = document.createElement("div");
    tip.className = "graph-tip";
    wrap.appendChild(tip);
    container.appendChild(wrap);

    const defs = el("defs", {}, svg);
    const mk = (id, color) => {
      const m = el("marker", { id, viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" }, defs);
      el("path", { d: "M0,0 L10,5 L0,10 z", fill: color }, m);
    };
    const uid = "g" + Math.random().toString(36).slice(2, 7);
    mk(uid + "a", "rgba(255,255,255,.28)");
    mk(uid + "h", "#c8ff4d");
    mk(uid + "f", "#3ef0e0");

    const gEdges = el("g", {}, svg), gLabels = el("g", {}, svg), gTemp = el("g", {}, svg), gNodes = el("g", {}, svg), gFx = el("g", {}, svg);
    const edges = new Map();
    groups.forEach((g, key) => {
      const rev = groups.has(`${g.dst}>${g.src}`);
      const k = rev ? (g.src < g.dst ? -9 : 9) : 0;
      const d = route(g.src, g.dst, k);
      const dashed = g.items.every((t) => /step_failed|exhausted|invalid|missing/.test(t.event));
      const path = el("path", { d, class: "g-edge" + (dashed ? " dashed" : ""), "marker-end": `url(#${uid}a)` }, gEdges);
      const len = path.getTotalLength ? path.getTotalLength() : 0;
      let mid = [0, 0];
      try { const pt = path.getPointAtLength(len / 2); mid = [pt.x, pt.y]; } catch (e) { /* 未挂载时忽略 */ }
      const txt = g.items.map((t) => t.event).join(" / ");
      const lab = el("text", { x: mid[0], y: mid[1] - 7, "text-anchor": "middle", class: "g-elabel" + (g.items.some((t) => MAIN.has(t.event)) ? "" : " hide") }, gLabels);
      lab.textContent = txt;
      edges.set(key, { path, lab, g, main: g.items.some((t) => MAIN.has(t.event)) });
    });
    // 暂停后重试：回到断点（自环示意）
    const sp = POS.SUSPENDED;
    const loop = el("path", { d: `M${sp[0] - W / 2},${sp[1] - 8} C${sp[0] - 150},${sp[1] - 40} ${sp[0] - 150},${sp[1] + 40} ${sp[0] - W / 2},${sp[1] + 10}`, class: "g-edge dashed", "marker-end": `url(#${uid}a)` }, gEdges);
    const loopLab = el("text", { x: sp[0] - 150, y: sp[1] + 4, "text-anchor": "end", class: "g-elabel" }, gLabels);
    loopLab.textContent = "retry → 回到断点";
    const cl = POS.CANCELLED;
    const cancelLab = el("text", { x: cl[0], y: cl[1] + H / 2 + 20, "text-anchor": "middle", class: "g-elabel" }, gLabels);
    cancelLab.textContent = "任一非终态 → cancel";

    const nodes = new Map();
    Object.entries(POS).forEach(([s, [x, y]]) => {
      const g = el("g", { class: "g-node", "data-s": s, transform: `translate(${x},${y})`, tabindex: "0" }, gNodes);
      el("rect", { class: "halo", x: -W / 2, y: -H / 2, width: W, height: H, rx: 16 }, g);
      el("rect", { x: -W / 2, y: -H / 2, width: W, height: H, rx: 16 }, g);
      const t1 = el("text", { y: -7 }, g);
      t1.textContent = labels[s] || s;
      const t2 = el("text", { y: 13, class: "code" }, g);
      t2.textContent = s;
      nodes.set(s, g);
      const show = () => focus(s);
      g.addEventListener("pointerenter", show);
      g.addEventListener("focus", show);
      g.addEventListener("pointerleave", unfocus);
      g.addEventListener("blur", unfocus);
    });

    function focus(s) {
      const out = meta.transitions.filter((t) => t.src === s);
      edges.forEach((e, key) => {
        const on = e.g.src === s;
        const inc = e.g.dst === s;
        e.path.classList.toggle("focus", on);
        e.path.classList.toggle("dim", !on && !inc);
        e.lab.classList.toggle("show", on);
        e.lab.classList.toggle("hide", !on && !e.main);
        if (on) e.path.setAttribute("marker-end", `url(#${uid}f)`);
      });
      const [x, y] = POS[s];
      const box = svg.getBoundingClientRect();
      const sc = box.width / 1220;
      tip.innerHTML = `<h5>${esc(labels[s] || s)} <span class="mono faint">${s}</span></h5>` +
        (out.length ? `<ul>${out.map((t) => `<li><b>${esc(t.event)}</b> → ${t.dst ? esc(labels[t.dst]) : "暂停前的状态（断点）"} · ${t.actors.map((a) => ACTOR[a] || a).join("/")}${t.desc ? `<br>${esc(t.desc)}` : ""}</li>`).join("")}</ul>`
          : `<ul><li>终态，不再流转</li></ul>`);
      const left = clamp((x + W / 2 + 12) * sc, 0, box.width - 300);
      const top = (y - 10 - 20) * sc;
      tip.style.left = (x > 800 ? Math.max(0, (x - W / 2 - 12) * sc - 320) : left) + "px";
      tip.style.top = Math.max(0, top) + "px";
      tip.classList.add("on");
    }
    function unfocus() {
      edges.forEach((e) => {
        e.path.classList.remove("focus", "dim");
        e.lab.classList.remove("show");
        e.lab.classList.toggle("hide", !e.main);
        e.path.setAttribute("marker-end", e.path.classList.contains("hot") ? `url(#${uid}h)` : `url(#${uid}a)`);
      });
      tip.classList.remove("on");
    }

    function edgeFor(src, dst) {
      const key = `${src}>${dst}`;
      if (edges.has(key)) return edges.get(key).path;
      // 不在图上的流转（重试回到断点、取消），临时画一条
      const d = route(src, dst, 0);
      const path = el("path", { d, class: "g-edge dashed", "marker-end": `url(#${uid}a)` }, gTemp);
      edges.set(key, { path, lab: el("text", { class: "g-elabel hide" }, gLabels), g: { src, dst, items: [] }, main: false, temp: true });
      return path;
    }

    let current = null;
    function setPath(steps, cur) {
      // steps: [[from, to], ...]
      nodes.forEach((g) => g.classList.remove("visited", "current"));
      edges.forEach((e) => {
        e.path.classList.remove("trail", "hot");
        e.path.setAttribute("marker-end", `url(#${uid}a)`);
      });
      const seen = new Set();
      steps.forEach(([a, b], i) => {
        seen.add(a); seen.add(b);
        if (!POS[a] || !POS[b]) return;
        const p = edgeFor(a, b);
        p.classList.add(i === steps.length - 1 ? "hot" : "trail");
        if (i === steps.length - 1) p.setAttribute("marker-end", `url(#${uid}h)`);
      });
      if (cur) seen.add(cur);
      seen.forEach((s) => nodes.get(s) && nodes.get(s).classList.add("visited"));
      current = cur;
      if (cur && nodes.get(cur)) nodes.get(cur).classList.add("current");
    }

    function comet(src, dst, dur = 800) {
      if (!POS[src] || !POS[dst] || FX.reduce) return;
      const p = edgeFor(src, dst);
      const len = p.getTotalLength();
      const c = el("circle", { r: 5, class: "g-comet" }, gFx);
      const tail = [];
      for (let i = 0; i < 6; i++) tail.push(el("circle", { r: 4 - i * 0.55, fill: "#c8ff4d", opacity: 0.5 - i * 0.07 }, gFx));
      const t0 = performance.now();
      const hist = [];
      const step = (now) => {
        const q = Math.min(1, (now - t0) / dur);
        const e = q < 0.5 ? 2 * q * q : 1 - Math.pow(-2 * q + 2, 2) / 2;
        const pt = p.getPointAtLength(e * len);
        c.setAttribute("cx", pt.x); c.setAttribute("cy", pt.y);
        hist.unshift([pt.x, pt.y]);
        tail.forEach((t, i) => { const h = hist[Math.min(hist.length - 1, (i + 1) * 2)]; t.setAttribute("cx", h[0]); t.setAttribute("cy", h[1]); });
        if (q < 1) requestAnimationFrame(step);
        else {
          c.remove(); tail.forEach((t) => t.remove());
          const node = nodes.get(dst);
          if (node) { node.style.transition = "transform .5s cubic-bezier(.34,1.56,.64,1)"; node.setAttribute("transform", `translate(${POS[dst][0]},${POS[dst][1]}) scale(1.08)`); setTimeout(() => node.setAttribute("transform", `translate(${POS[dst][0]},${POS[dst][1]})`), 260); }
        }
      };
      requestAnimationFrame(step);
    }

    // 首页自动演示：沿一条典型路径循环
    let demoTimer = 0, demoOn = false;
    function demo(seq) {
      seq = seq || ["CREATED", "ADMISSION", "RUNNING", "QC", "AUTO_FIXING", "QC", "PENDING_REVIEW", "REWORK", "QC", "PENDING_REVIEW", "DELIVERED"];
      demoOn = true;
      let i = 0;
      const steps = [];
      const tick = () => {
        if (!demoOn) return;
        if (i >= seq.length - 1) {
          demoTimer = setTimeout(() => { i = 0; steps.length = 0; setPath([], seq[0]); demoTimer = setTimeout(tick, 900); }, 2600);
          return;
        }
        const a = seq[i], b = seq[i + 1];
        comet(a, b, 750);
        setTimeout(() => { steps.push([a, b]); setPath(steps, b); if (opts.onStep) opts.onStep(b); }, 720);
        i++;
        demoTimer = setTimeout(tick, 1250);
      };
      setPath([], seq[0]);
      const io = new IntersectionObserver((es) => {
        if (es[0].isIntersecting && !demoTimer) tick();
        else if (!es[0].isIntersecting) { clearTimeout(demoTimer); demoTimer = 0; }
      });
      io.observe(wrap);
    }

    return {
      el: wrap, setPath, comet,
      demo, stopDemo() { demoOn = false; clearTimeout(demoTimer); },
      get current() { return current; },
    };
  }

  // 从事件流中取出状态轨迹
  function pathFromEvents(events) {
    return events.filter((e) => e.type === "state_changed").map((e) => [e.payload.from, e.payload.to]);
  }

  function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

  window.Graph = { create, pathFromEvents, POS };
})();
