/* 动态图表（SVG / CSS），只展示程序已算好的数，不在界面里重新计算 */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const int = (v) => (v == null || !isFinite(v) ? "—" : Math.round(v).toLocaleString("en-US"));
  const pct = (v) => (v == null || !isFinite(v) ? "—" : (v * 100).toFixed(1) + "%");
  const spct = (v) => (v == null || !isFinite(v) ? "—" : (v > 0 ? "+" : "") + (v * 100).toFixed(1) + "%");
  const wan = (v) => (v == null ? "—" : (v / 10000).toFixed(1) + "万");
  const chip = (v) => (v == null || !isFinite(v) ? `<span class="chip">—</span>` : `<span class="chip ${v < 0 ? "down" : "up"}">${v < 0 ? "↓" : "↑"} ${spct(v)}</span>`);

  /* 区域实收与目标 */
  function regions(host, rows) {
    const max = Math.max(...rows.map((r) => Math.max(r.sales || 0, r.target || 0)), 1) * 1.04;
    host.innerHTML = `<div class="bars-h" data-in>${rows.map((r, i) => {
      const rate = r.target_rate;
      const cls = rate == null ? "" : rate < 0.9 ? "low" : rate < 1 ? "mid" : "";
      return `<div class="bar-row">
        <div class="bn">${esc(r.name)}</div>
        <div class="track"><div class="fill ${cls}" style="--w:${((r.sales || 0) / max) * 100}%;--d:${i * 120}ms"></div>
          ${r.target ? `<div class="target" style="--t:${(r.target / max) * 100}%"></div>` : ""}</div>
        <div class="bv"><b class="num">${int(r.sales)}</b><span class="${(r.sales_wow ?? 0) < 0 ? "faint" : "faint"}">${spct(r.sales_wow)} · 达成 ${pct(rate)}</span></div>
      </div>`;
    }).join("")}</div>`;
    FX.bind(host);
  }

  /* 近 4 周趋势：平滑曲线 + 面积 + 目标虚线 */
  function trend(host, weekly) {
    if (!weekly || !weekly.length) { host.innerHTML = `<p class="faint">数据不足 4 周</p>`; return; }
    const Wd = 620, Ht = 250, pl = 50, pr = 24, pt = 34, pb = 36;
    const vals = weekly.map((w) => w.sales).concat(weekly.map((w) => w.target).filter(Boolean));
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const padv = (hi - lo) * 0.25 || hi * 0.1;
    lo -= padv; hi += padv;
    const x = (i) => pl + (weekly.length === 1 ? (Wd - pl - pr) / 2 : (i * (Wd - pl - pr)) / (weekly.length - 1));
    const y = (v) => pt + (1 - (v - lo) / (hi - lo)) * (Ht - pt - pb);
    const pts = weekly.map((w, i) => [x(i), y(w.sales)]);
    const smooth = (p) => {
      if (p.length < 2) return `M${p[0][0]},${p[0][1]}`;
      let d = `M${p[0][0]},${p[0][1]}`;
      for (let i = 0; i < p.length - 1; i++) {
        const p0 = p[i - 1] || p[i], p1 = p[i], p2 = p[i + 1], p3 = p[i + 2] || p2;
        const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
        const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
        d += ` C${c1[0].toFixed(1)},${c1[1].toFixed(1)} ${c2[0].toFixed(1)},${c2[1].toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
      }
      return d;
    };
    const line = smooth(pts);
    const area = `${line} L${pts[pts.length - 1][0]},${Ht - pb} L${pts[0][0]},${Ht - pb} Z`;
    const tg = weekly.filter((w) => w.target).length === weekly.length ? weekly.map((w, i) => `${x(i)},${y(w.target)}`).join(" ") : "";
    const ticks = [0, 1, 2, 3].map((k) => lo + ((hi - lo) * k) / 3);
    const id = "t" + Math.random().toString(36).slice(2, 7);
    host.innerHTML = `<div class="chart" data-in><svg viewBox="0 0 ${Wd} ${Ht}">
      <defs><linearGradient id="${id}g" x1="0" x2="1"><stop offset="0" stop-color="#8b6cff"/><stop offset=".5" stop-color="#3ef0e0"/><stop offset="1" stop-color="#c8ff4d"/></linearGradient>
      <linearGradient id="${id}a" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#3ef0e0" stop-opacity=".28"/><stop offset="1" stop-color="#3ef0e0" stop-opacity="0"/></linearGradient></defs>
      ${ticks.map((v) => `<line class="gridline" x1="${pl}" x2="${Wd - pr}" y1="${y(v)}" y2="${y(v)}"/><text x="${pl - 10}" y="${y(v) + 4}" text-anchor="end" font-size="11">${wan(v)}</text>`).join("")}
      <path class="trend-area" d="${area}" fill="url(#${id}a)"/>
      ${tg ? `<polyline class="trend-target" points="${tg}"/><text x="${Wd - pr}" y="${y(weekly[weekly.length - 1].target) - 8}" text-anchor="end" font-size="11" fill="#6c7590">目标</text>` : ""}
      <path class="trend-path" d="${line}" stroke="url(#${id}g)"/>
      ${pts.map((p, i) => `<g><circle class="trend-dot" cx="${p[0]}" cy="${p[1]}" r="6" style="transition-delay:${600 + i * 160}ms"/>
        <text x="${p[0]}" y="${p[1] - 16}" text-anchor="middle" font-size="12" fill="#eef1f8" font-weight="700" opacity="0" style="transition:opacity .6s ${800 + i * 160}ms" class="tv">${wan(weekly[i].sales)}</text>
        <text x="${p[0]}" y="${Ht - 10}" text-anchor="middle" font-size="12">${esc(weekly[i].label)}</text></g>`).join("")}
    </svg></div>`;
    const ch = host.querySelector(".chart");
    const path = host.querySelector(".trend-path");
    const len = path.getTotalLength();
    path.style.strokeDasharray = len;
    path.style.strokeDashoffset = len;
    ch.addEventListener("reveal", () => {
      path.style.transition = "stroke-dashoffset 1.8s cubic-bezier(.22,1,.36,1)";
      path.style.strokeDashoffset = 0;
      host.querySelectorAll(".tv").forEach((t) => (t.style.opacity = 1));
    });
    FX.bind(host);
  }

  /* 品类结构：环形图 */
  const PAL = ["#c8ff4d", "#3ef0e0", "#8b6cff", "#ffb547", "#ff5c7a", "#4f8bff", "#e9ecf5"];
  function donut(host, cats) {
    const total = cats.reduce((s, c) => s + Math.max(0, c.sales || 0), 0) || 1;
    const R = 78, C = 2 * Math.PI * R;
    let acc = 0;
    const segs = cats.map((c, i) => {
      const len = (Math.max(0, c.sales || 0) / total) * C;
      const s = { len, off: acc, color: PAL[i % PAL.length], c };
      acc += len;
      return s;
    });
    host.innerHTML = `<div class="donut-wrap" data-in>
      <svg viewBox="0 0 200 200" style="width:200px;max-width:100%">
        <circle cx="100" cy="100" r="${R}" fill="none" stroke="rgba(255,255,255,.05)" stroke-width="22"/>
        ${segs.map((s, i) => `<circle class="donut-seg" cx="100" cy="100" r="${R}" stroke="${s.color}" transform="rotate(-90 100 100)"
          stroke-dasharray="0 ${C}" stroke-dashoffset="${-s.off}" data-len="${Math.max(0, s.len - 2)}" style="--d:${i * 140}ms;transition:stroke-dasharray 1.3s cubic-bezier(.22,1,.36,1) ${i * 140}ms, stroke-width .3s"><title>${esc(s.c.name)} ${pct(s.c.share)}</title></circle>`).join("")}
        <text x="100" y="94" text-anchor="middle" font-size="12" fill="#6c7590">本周实收</text>
        <text x="100" y="118" text-anchor="middle" font-size="22" font-weight="800" fill="#eef1f8">${wan(total)}</text>
      </svg>
      <div class="donut-legend">${segs.map((s) => `<div><i style="background:${s.color}"></i><span>${esc(s.c.name)}</span><span class="num faint">${pct(s.c.share)}</span>${chip(s.c.sales_wow)}</div>`).join("")}</div>
    </div>`;
    const wrap = host.querySelector(".donut-wrap");
    wrap.addEventListener("reveal", () => {
      wrap.querySelectorAll(".donut-seg").forEach((c) => c.setAttribute("stroke-dasharray", `${c.dataset.len} ${C}`));
    });
    FX.bind(host);
  }

  /* 异常对象：以 0 为轴的发散条形 */
  function diverging(host, items) {
    if (!items || !items.length) { host.innerHTML = `<p class="muted">本周没有环比超过阈值的区域或门店。</p>`; return; }
    const max = Math.max(...items.flatMap((a) => [a.wow].concat((a.drivers || []).map((d) => d.wow))).map((v) => Math.abs(v || 0)), 0.01);
    host.innerHTML = `<div class="diverging" data-in>${items.map((a, i) => {
      const w = (Math.abs(a.wow || 0) / max) * 46;
      const dir = (a.wow || 0) < 0 ? "down" : "up";
      return `<div class="div-row"><div class="dn" title="${esc(a.label)}"><span class="mono faint">${esc(a.id)}</span> ${esc(a.label)}</div>
        <div class="div-track"><div class="div-bar ${dir}" style="width:${w}%;--d:${i * 110}ms"><span>${spct(a.wow)} · ${a.impact > 0 ? "+" : ""}${int(a.impact)}</span></div></div></div>
        ${(a.drivers || []).map((d) => `<div class="div-row" style="opacity:.7"><div class="dn">　└ ${esc(d.label)}</div><div class="div-track"><div class="div-bar ${(d.wow || 0) < 0 ? "down" : "up"}" style="width:${(Math.abs(d.wow || 0) / max) * 46}%;height:auto;top:8px;bottom:8px;--d:${i * 110 + 200}ms"><span>${spct(d.wow)}</span></div></div></div>`).join("")}`;
    }).join("")}</div>`;
    FX.bind(host);
  }

  /* 每日对比：本周 vs 上周 */
  function daily(host, days) {
    if (!days || !days.length) { host.innerHTML = ""; return; }
    const max = Math.max(...days.map((d) => Math.max(d.sales, d.sales_prev)), 1);
    host.innerHTML = `<div class="dual" data-in>${days.map((d, i) => `<div class="day" title="${esc(d.date)} 本周 ${int(d.sales)} / 上周 ${int(d.sales_prev)}">
      <div class="cols"><div class="col prev" style="--h:${(d.sales_prev / max) * 100}%;--d:${i * 70}ms"></div><div class="col cur" style="--h:${(d.sales / max) * 100}%;--d:${i * 70 + 60}ms"></div></div>
      <div class="lbl">周${esc(d.weekday)}</div></div>`).join("")}</div>
      <div class="legend-inline" style="margin-top:12px"><span><i style="background:rgba(255,255,255,.16)"></i>上周</span><span><i style="background:var(--lime)"></i>本周</span></div>`;
    FX.bind(host);
  }

  window.Charts = { regions, trend, donut, diverging, daily, int, pct, spct, wan, chip };
})();
