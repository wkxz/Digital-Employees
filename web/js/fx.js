/* 动效引擎：WebGL 流光背景、粒子核心、光标、磁吸、倾斜、文字拆分、数字滚动、粒子爆发、故障闪屏、提示 */
(function () {
  "use strict";
  const FX = {};
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const fine = matchMedia("(pointer: fine)").matches;
  const lerp = (a, b, t) => a + (b - a) * t;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const mouse = { x: innerWidth / 2, y: innerHeight / 2, nx: 0.5, ny: 0.5 };
  Object.assign(FX, { reduce, fine, lerp, clamp, mouse });

  addEventListener("pointermove", (e) => {
    mouse.x = e.clientX; mouse.y = e.clientY;
    mouse.nx = e.clientX / innerWidth; mouse.ny = e.clientY / innerHeight;
  }, { passive: true });

  /* ------------------------------------------------------------ WebGL 流光背景（随任务状态换色） */
  const MOODS = {
    idle:    [[0.36, 0.25, 1.00], [0.12, 0.90, 0.85], [0.05, 0.07, 0.22]],
    running: [[0.12, 0.62, 1.00], [0.24, 0.98, 0.88], [0.03, 0.09, 0.22]],
    waiting: [[1.00, 0.66, 0.22], [0.58, 0.32, 1.00], [0.12, 0.07, 0.12]],
    danger:  [[1.00, 0.30, 0.44], [0.60, 0.22, 0.90], [0.14, 0.04, 0.09]],
    success: [[0.76, 1.00, 0.28], [0.20, 0.96, 0.82], [0.03, 0.11, 0.09]],
    review:  [[0.56, 0.42, 1.00], [0.98, 0.42, 0.82], [0.08, 0.05, 0.20]],
  };
  const VERT = "attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}";
  const FRAG = `precision mediump float;
uniform vec2 uRes;uniform float uTime;uniform vec2 uMouse;uniform vec3 uA;uniform vec3 uB;uniform vec3 uC;uniform float uPulse;uniform float uScroll;
float hash(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}
float noise(vec2 p){vec2 i=floor(p),f=fract(p);vec2 u=f*f*(3.-2.*f);
return mix(mix(hash(i),hash(i+vec2(1.,0.)),u.x),mix(hash(i+vec2(0.,1.)),hash(i+vec2(1.,1.)),u.x),u.y);}
float fbm(vec2 p){float v=0.,a=.5;mat2 m=mat2(1.6,1.2,-1.2,1.6);for(int i=0;i<5;i++){v+=a*noise(p);p=m*p;a*=.5;}return v;}
void main(){
  vec2 p=(gl_FragCoord.xy-.5*uRes.xy)/uRes.y;
  float t=uTime*.05;
  vec2 m=(uMouse-.5)*vec2(uRes.x/uRes.y,1.);
  p+=m*.06; p.y+=uScroll*.15;
  vec2 q=vec2(fbm(p*1.3+vec2(0.,t)),fbm(p*1.3+vec2(5.2,1.3-t)));
  vec2 r=vec2(fbm(p*1.5+3.*q+vec2(1.7,9.2)+t*1.3),fbm(p*1.5+3.*q+vec2(8.3,2.8)-t));
  float f=fbm(p*1.1+2.6*r);
  vec3 col=vec3(.010,.013,.030);
  col=mix(col,uC,smoothstep(.15,.95,f));
  col=mix(col,uA,smoothstep(.5,1.05,f)*.62*(.55+.45*q.x));
  col=mix(col,uB,smoothstep(.58,1.1,r.y)*.42);
  float d=length(p-m*vec2(1.,-1.));
  col+=uB*.07*smoothstep(.7,0.,d);
  col+=uA*uPulse*.45*smoothstep(1.4,0.,length(p));
  float vig=smoothstep(1.35,.2,length(p*vec2(.8,1.05)));
  col*=.62*vig+.06;
  gl_FragColor=vec4(col,1.);
}`;
  FX.initBackground = function (canvas) {
    const state = { cur: MOODS.idle.map((c) => c.slice()), target: MOODS.idle, pulse: 0, scroll: 0 };
    FX.mood = (name) => { state.target = MOODS[name] || MOODS.idle; };
    FX.pulse = (v = 1) => { state.pulse = Math.max(state.pulse, v); };
    let gl;
    try { gl = canvas.getContext("webgl", { antialias: false, alpha: false, powerPreference: "low-power" }); } catch (e) { gl = null; }
    if (!gl) {
      document.body.style.background = "radial-gradient(1200px 700px at 70% 0%, #1b1450 0%, transparent 60%), radial-gradient(900px 600px at 0% 100%, #06323a 0%, transparent 60%), #05060b";
      return;
    }
    const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
    const prog = gl.createProgram();
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) { console.warn("shader link failed", gl.getProgramInfoLog(prog)); return; }
    gl.useProgram(prog);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(prog, "p");
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    const U = {};
    ["uRes", "uTime", "uMouse", "uA", "uB", "uC", "uPulse", "uScroll"].forEach((n) => (U[n] = gl.getUniformLocation(prog, n)));
    const scale = 0.5;
    const resize = () => {
      canvas.width = Math.max(1, Math.round(innerWidth * scale));
      canvas.height = Math.max(1, Math.round(innerHeight * scale));
      gl.viewport(0, 0, canvas.width, canvas.height);
    };
    resize();
    addEventListener("resize", resize);
    const sm = { x: 0.5, y: 0.5 };
    const t0 = performance.now();
    let last = 0;
    const frame = (now) => {
      requestAnimationFrame(frame);
      if (document.hidden) return;
      if (now - last < 1000 / 45) return; // 背景 45fps 足够
      last = now;
      sm.x = lerp(sm.x, mouse.nx, 0.04); sm.y = lerp(sm.y, mouse.ny, 0.04);
      for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) state.cur[i][j] = lerp(state.cur[i][j], state.target[i][j], 0.03);
      state.pulse *= 0.955;
      state.scroll = lerp(state.scroll, scrollY / Math.max(1, document.body.scrollHeight - innerHeight), 0.05);
      gl.uniform2f(U.uRes, canvas.width, canvas.height);
      gl.uniform1f(U.uTime, reduce ? 12 : (now - t0) / 1000);
      gl.uniform2f(U.uMouse, sm.x, sm.y);
      gl.uniform3fv(U.uA, state.cur[0]); gl.uniform3fv(U.uB, state.cur[1]); gl.uniform3fv(U.uC, state.cur[2]);
      gl.uniform1f(U.uPulse, state.pulse);
      gl.uniform1f(U.uScroll, state.scroll || 0);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    };
    requestAnimationFrame(frame);
  };
  FX.mood = () => {};
  FX.pulse = () => {};

  /* ------------------------------------------------------------ 胶片颗粒 */
  FX.initGrain = function (el) {
    const c = document.createElement("canvas");
    c.width = c.height = 160;
    const ctx = c.getContext("2d");
    const img = ctx.createImageData(160, 160);
    for (let i = 0; i < img.data.length; i += 4) {
      const v = (Math.random() * 255) | 0;
      img.data[i] = img.data[i + 1] = img.data[i + 2] = v;
      img.data[i + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
    el.style.backgroundImage = `url(${c.toDataURL()})`;
  };

  /* ------------------------------------------------------------ 自定义光标 */
  FX.initCursor = function () {
    if (!fine || reduce) return;
    const c = document.querySelector(".cursor");
    if (!c) return;
    document.documentElement.classList.add("has-cursor");
    const dot = c.querySelector(".cursor-dot"), ring = c.querySelector(".cursor-ring"), label = c.querySelector(".cursor-label");
    let rx = mouse.x, ry = mouse.y, rs = 1, shown = false;
    const loop = () => {
      rx = lerp(rx, mouse.x, 0.2); ry = lerp(ry, mouse.y, 0.2);
      // 按下时缩小写进同一个 transform（先平移再缩放），单独用 CSS scale 会连位移一起缩放，按住拖选文字时光圈偏离鼠标
      rs = lerp(rs, c.classList.contains("is-down") ? 0.82 : 1, 0.25);
      dot.style.transform = `translate(${mouse.x}px,${mouse.y}px)`;
      ring.style.transform = `translate(${rx}px,${ry}px) scale(${rs})`;
      requestAnimationFrame(loop);
    };
    loop();
    c.style.opacity = "0";
    addEventListener("pointermove", () => { if (!shown) { shown = true; c.style.opacity = "1"; } }, { passive: true });
    document.documentElement.addEventListener("mouseleave", () => { c.style.opacity = "0"; shown = false; });
    const INTER = "a, button, [role=button], .drop, .chip-btn, .task-card, summary, .g-node, .toggle, .feed-row, .dl-item, .rule-pill";
    document.addEventListener("pointerover", (e) => {
      const t = e.target instanceof Element ? e.target : null;
      const native = t && t.closest("input, textarea, select, .editor");
      const lab = t && t.closest("[data-cursor]");
      const inter = t && t.closest(INTER);
      c.classList.toggle("is-native", !!native);
      c.classList.toggle("is-label", !!lab && !native);
      c.classList.toggle("is-hover", !!inter && !lab && !native);
      if (lab) label.textContent = lab.getAttribute("data-cursor");
    });
    addEventListener("pointerdown", () => c.classList.add("is-down"));
    addEventListener("pointerup", () => c.classList.remove("is-down"));
  };

  /* ------------------------------------------------------------ 聚光边框（全局委托） */
  document.addEventListener("pointermove", (e) => {
    const card = e.target instanceof Element ? e.target.closest(".spot") : null;
    if (!card) return;
    const r = card.getBoundingClientRect();
    card.style.setProperty("--mx", `${e.clientX - r.left}px`);
    card.style.setProperty("--my", `${e.clientY - r.top}px`);
  }, { passive: true });

  /* ------------------------------------------------------------ 磁吸、倾斜、涟漪 */
  FX.magnetic = function (el, strength = 0.35) {
    if (!fine || reduce || el._mag) return;
    el._mag = true;
    const inner = el.querySelector(".roll, .mag-inner");
    el.addEventListener("pointermove", (e) => {
      const r = el.getBoundingClientRect();
      const x = e.clientX - r.left - r.width / 2, y = e.clientY - r.top - r.height / 2;
      el.style.transform = `translate(${x * strength}px, ${y * strength}px)`;
      if (inner) inner.style.transform = `translate(${x * strength * 0.4}px, ${y * strength * 0.4}px)`;
    });
    el.addEventListener("pointerleave", () => {
      el.style.transform = "";
      if (inner) inner.style.transform = "";
    });
  };
  FX.tilt = function (el, max = 7) {
    if (!fine || reduce || el._tilt) return;
    el._tilt = true;
    el.addEventListener("pointermove", (e) => {
      const r = el.getBoundingClientRect();
      const px = (e.clientX - r.left) / r.width - 0.5, py = (e.clientY - r.top) / r.height - 0.5;
      el.style.transition = "transform .15s ease-out";
      el.style.transform = `perspective(900px) rotateX(${-py * max}deg) rotateY(${px * max}deg) translateZ(0)`;
    });
    el.addEventListener("pointerleave", () => {
      el.style.transition = "";
      el.style.transform = "";
    });
  };
  document.addEventListener("pointerdown", (e) => {
    const btn = e.target instanceof Element ? e.target.closest(".btn") : null;
    if (!btn || reduce) return;
    const r = btn.getBoundingClientRect();
    const s = Math.max(r.width, r.height) * 2.2;
    const sp = document.createElement("span");
    sp.className = "ripple";
    sp.style.cssText = `width:${s}px;height:${s}px;left:${e.clientX - r.left - s / 2}px;top:${e.clientY - r.top - s / 2}px`;
    btn.appendChild(sp);
    setTimeout(() => sp.remove(), 750);
  });

  /* ------------------------------------------------------------ 文字拆分 */
  FX.split = function (el) {
    if (el.dataset.splitDone) return;
    el.dataset.splitDone = "1";
    el.classList.add("split");
    let i = 0;
    const base = parseInt(el.dataset.delay || "0", 10);
    if (base) el.style.setProperty("--d", base + "ms");
    // 渐变文字整体作为一个单元升起，渐变才连贯
    const whole = (host) => {
      const w = document.createElement("span");
      w.className = "w";
      const c = document.createElement("span");
      c.className = "c";
      c.textContent = host.textContent;
      c.style.setProperty("--i", i++);
      w.appendChild(c);
      host.textContent = "";
      host.appendChild(w);
    };
    if (el.classList.contains("gradient-text")) { whole(el); return; }
    const walk = (node) => {
      [...node.childNodes].forEach((n) => {
        if (n.nodeType === 1 && n.classList.contains("hl")) { whole(n); return; }
        if (n.nodeType === 3) {
          const frag = document.createDocumentFragment();
          n.textContent.split(/(\s+)/).forEach((part) => {
            if (!part) return;
            if (/^\s+$/.test(part)) {
              const sp = document.createElement("span");
              sp.className = "sp";
              frag.appendChild(sp);
              return;
            }
            const w = document.createElement("span");
            w.className = "w";
            for (const ch of part) {
              const c = document.createElement("span");
              c.className = "c";
              c.textContent = ch;
              c.style.setProperty("--i", i++);
              w.appendChild(c);
            }
            frag.appendChild(w);
          });
          n.replaceWith(frag);
        } else if (n.nodeType === 1 && n.tagName !== "BR") {
          walk(n);
        }
      });
    };
    walk(el);
  };

  /* ------------------------------------------------------------ 出现动画 */
  const io = new IntersectionObserver((entries) => {
    entries.forEach((en) => {
      if (!en.isIntersecting) return;
      const el = en.target;
      io.unobserve(el);
      el.classList.add("in");
      if (el.dataset.count !== undefined) FX.countEl(el);
      el.dispatchEvent(new CustomEvent("reveal"));
    });
  }, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
  FX.observe = (el) => io.observe(el);

  FX.bind = function (root) {
    root.querySelectorAll("[data-split]").forEach((el) => { FX.split(el); io.observe(el); });
    root.querySelectorAll("[data-reveal], [data-in], [data-count]").forEach((el) => io.observe(el));
    root.querySelectorAll("[data-magnetic]").forEach((el) => FX.magnetic(el, parseFloat(el.dataset.magnetic) || 0.35));
    root.querySelectorAll("[data-tilt]").forEach((el) => FX.tilt(el, parseFloat(el.dataset.tilt) || 7));
  };

  /* ------------------------------------------------------------ 数字滚动 */
  const FMT = {
    int: (v) => Math.round(v).toLocaleString("en-US"),
    pct1: (v) => (v * 100).toFixed(1) + "%",
    spct1: (v) => (v > 0 ? "+" : "") + (v * 100).toFixed(1) + "%",
    dec1: (v) => v.toFixed(1),
    raw: (v) => String(Math.round(v)),
  };
  FX.FMT = FMT;
  FX.count = function (el, to, fmt = "int", dur = 1700) {
    const f = typeof fmt === "function" ? fmt : FMT[fmt] || FMT.int;
    if (reduce || !isFinite(to)) { el.textContent = isFinite(to) ? f(to) : "—"; return; }
    const from = 0, t0 = performance.now();
    const step = (now) => {
      const p = clamp((now - t0) / dur, 0, 1);
      const e = p === 1 ? 1 : 1 - Math.pow(2, -10 * p);
      el.textContent = f(from + (to - from) * e);
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };
  FX.countEl = (el) => FX.count(el, parseFloat(el.dataset.count), el.dataset.fmt || "int", parseInt(el.dataset.dur || "1700", 10));

  /* ------------------------------------------------------------ 乱码解码、打字机 */
  const GLYPHS = "▚▞▙▟░▒▓<>/\\=+*#01";
  FX.scramble = function (el, text, dur = 650) {
    if (reduce) { el.textContent = text; return; }
    const t0 = performance.now(), len = text.length;
    const step = (now) => {
      const p = clamp((now - t0) / dur, 0, 1);
      let out = "";
      for (let i = 0; i < len; i++) out += i < p * len ? text[i] : GLYPHS[(Math.random() * GLYPHS.length) | 0];
      el.textContent = p >= 1 ? text : out;
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };
  FX.type = function (el, text, cps = 60) {
    if (reduce) { el.textContent = text; return; }
    el.textContent = "";
    let i = 0;
    const id = setInterval(() => {
      i += Math.max(1, Math.round(cps / 30));
      el.textContent = text.slice(0, i);
      if (i >= text.length) clearInterval(id);
    }, 33);
  };

  /* ------------------------------------------------------------ 粒子核心（数字员工的“心脏”） */
  FX.orb = function (canvas, opts = {}) {
    const ctx = canvas.getContext("2d");
    const N = opts.n || 900;
    const pts = [];
    for (let i = 0; i < N; i++) {
      const y = 1 - (i / (N - 1)) * 2;
      const r = Math.sqrt(1 - y * y);
      const th = i * 2.399963229728653;
      pts.push([Math.cos(th) * r, y, Math.sin(th) * r, Math.random()]);
    }
    const st = { energy: opts.energy ?? 0.45, speed: opts.speed ?? 0.0035, c1: [200, 255, 77], c2: [62, 240, 224], rings: opts.rings !== false };
    const tgt = { energy: st.energy, speed: st.speed, c1: st.c1.slice(), c2: st.c2.slice() };
    let rotY = 0, rotX = 0.35, w = 0, h = 0, dpr = 1, alive = true, visible = true, waves = [];
    const resize = () => {
      dpr = Math.min(2, devicePixelRatio || 1);
      w = canvas.clientWidth; h = canvas.clientHeight;
      canvas.width = Math.max(1, w * dpr); canvas.height = Math.max(1, h * dpr);
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);
    const vio = new IntersectionObserver((es) => { visible = es[0].isIntersecting; });
    vio.observe(canvas);
    const t0 = performance.now();
    const draw = (now) => {
      if (!alive) return;
      requestAnimationFrame(draw);
      if (!visible || document.hidden || !w) return;
      const t = (now - t0) / 1000;
      st.energy = lerp(st.energy, tgt.energy, 0.04);
      st.speed = lerp(st.speed, tgt.speed, 0.04);
      for (let i = 0; i < 3; i++) { st.c1[i] = lerp(st.c1[i], tgt.c1[i], 0.05); st.c2[i] = lerp(st.c2[i], tgt.c2[i], 0.05); }
      const W = w * dpr, H = h * dpr, cx = W / 2, cy = H / 2, R = Math.min(W, H) * (opts.radius || 0.3);
      ctx.clearRect(0, 0, W, H);
      const follow = opts.follow !== false;
      rotY += reduce ? 0 : st.speed + (follow ? (mouse.nx - 0.5) * 0.006 : 0);
      rotX = lerp(rotX, 0.35 + (follow ? (mouse.ny - 0.5) * 0.7 : 0), 0.04);
      const cyR = Math.cos(rotY), syR = Math.sin(rotY), cxR = Math.cos(rotX), sxR = Math.sin(rotX);
      // 核心辉光
      const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 1.5);
      g.addColorStop(0, `rgba(${st.c2[0] | 0},${st.c2[1] | 0},${st.c2[2] | 0},${0.16 + st.energy * 0.14})`);
      g.addColorStop(0.45, `rgba(${st.c1[0] | 0},${st.c1[1] | 0},${st.c1[2] | 0},${0.05 + st.energy * 0.05})`);
      g.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, W, H);
      ctx.globalCompositeOperation = "lighter";
      // 轨道环
      if (st.rings) {
        for (let k = 0; k < 3; k++) {
          const a = t * (0.25 + k * 0.12) * (k % 2 ? -1 : 1);
          const rx = R * (1.32 + k * 0.17), ry = R * (0.3 + k * 0.08);
          ctx.save();
          ctx.translate(cx, cy);
          ctx.rotate(-0.42 + k * 0.5 + (mouse.nx - 0.5) * 0.2);
          ctx.strokeStyle = `rgba(255,255,255,${0.07 + st.energy * 0.05})`;
          ctx.lineWidth = dpr;
          ctx.beginPath();
          ctx.ellipse(0, 0, rx, ry, 0, 0, Math.PI * 2);
          ctx.stroke();
          const px = Math.cos(a) * rx, py = Math.sin(a) * ry;
          ctx.fillStyle = k === 1 ? `rgb(${st.c2.map((v) => v | 0)})` : `rgb(${st.c1.map((v) => v | 0)})`;
          ctx.beginPath();
          ctx.arc(px, py, 3.2 * dpr, 0, Math.PI * 2);
          ctx.fill();
          ctx.restore();
        }
      }
      // 粒子球
      for (let i = 0; i < N; i++) {
        const p = pts[i];
        const wob = 1 + st.energy * 0.14 * Math.sin(t * 2.2 + p[3] * 6.283 + p[1] * 5) + st.energy * 0.05 * Math.sin(t * 5.1 + p[0] * 7);
        let x = p[0] * wob, y = p[1] * wob, z = p[2] * wob;
        const x1 = x * cyR - z * syR, z1 = x * syR + z * cyR;
        const y2 = y * cxR - z1 * sxR, z2 = y * sxR + z1 * cxR;
        const s = 2.4 / (2.4 - z2);
        const X = cx + x1 * R * s, Y = cy + y2 * R * s;
        const depth = (z2 + 1) / 2;
        const a = 0.12 + depth * 0.88;
        const mix = (p[1] + 1) / 2;
        const r = st.c1[0] * mix + st.c2[0] * (1 - mix), gg = st.c1[1] * mix + st.c2[1] * (1 - mix), b = st.c1[2] * mix + st.c2[2] * (1 - mix);
        ctx.fillStyle = `rgba(${r | 0},${gg | 0},${b | 0},${a.toFixed(2)})`;
        const sz = (0.6 + depth * 1.9) * dpr * (opts.dot || 1);
        ctx.fillRect(X - sz / 2, Y - sz / 2, sz, sz);
      }
      // 脉冲波
      waves = waves.filter((wv) => wv.r < R * 2.6);
      waves.forEach((wv) => {
        wv.r += R * 0.03;
        ctx.strokeStyle = `rgba(${wv.c},${Math.max(0, 0.5 - wv.r / (R * 5.2))})`;
        ctx.lineWidth = 2 * dpr;
        ctx.beginPath();
        ctx.arc(cx, cy, wv.r, 0, Math.PI * 2);
        ctx.stroke();
      });
      ctx.globalCompositeOperation = "source-over";
    };
    requestAnimationFrame(draw);
    const PALETTE = {
      idle: [[200, 255, 77], [62, 240, 224], 0.45, 0.0035],
      running: [[62, 240, 224], [79, 139, 255], 0.9, 0.012],
      waiting: [[255, 181, 71], [139, 108, 255], 0.35, 0.002],
      danger: [[255, 92, 122], [255, 181, 71], 0.75, 0.0015],
      review: [[139, 108, 255], [62, 240, 224], 0.55, 0.004],
      success: [[200, 255, 77], [62, 240, 224], 0.6, 0.006],
      muted: [[125, 133, 151], [90, 96, 112], 0.2, 0.0015],
    };
    return {
      set(name) {
        const p = PALETTE[name] || PALETTE.idle;
        tgt.c1 = p[0].slice(); tgt.c2 = p[1].slice(); tgt.energy = p[2]; tgt.speed = p[3];
      },
      wave(color = "200,255,77") { waves.push({ r: 0, c: color }); },
      destroy() { alive = false; ro.disconnect(); vio.disconnect(); },
    };
  };

  /* ------------------------------------------------------------ 粒子爆发 */
  let fxCanvas, fxCtx, parts = [], rings = [], running = false;
  const ensureFx = () => {
    if (fxCanvas) return;
    fxCanvas = document.createElement("canvas");
    fxCanvas.id = "fx-canvas";
    document.body.appendChild(fxCanvas);
    fxCtx = fxCanvas.getContext("2d");
    const rs = () => { fxCanvas.width = innerWidth; fxCanvas.height = innerHeight; };
    rs();
    addEventListener("resize", rs);
  };
  const loopFx = () => {
    fxCtx.clearRect(0, 0, fxCanvas.width, fxCanvas.height);
    parts = parts.filter((p) => p.life > 0 && p.y < innerHeight + 40);
    parts.forEach((p) => {
      p.vx *= 0.985; p.vy = p.vy * 0.985 + p.g; p.x += p.vx; p.y += p.vy; p.r += p.vr; p.life -= p.decay;
      fxCtx.save();
      fxCtx.globalAlpha = Math.max(0, Math.min(1, p.life * 1.4));
      fxCtx.translate(p.x, p.y);
      fxCtx.rotate(p.r);
      fxCtx.fillStyle = p.c;
      if (p.circle) { fxCtx.beginPath(); fxCtx.arc(0, 0, p.w / 2.4, 0, Math.PI * 2); fxCtx.fill(); }
      else fxCtx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h * Math.abs(Math.cos(p.r * 2)));
      fxCtx.restore();
    });
    rings = rings.filter((r) => r.a > 0.01);
    rings.forEach((r) => {
      r.r += (r.max - r.r) * 0.08; r.a *= 0.93;
      fxCtx.strokeStyle = `rgba(${r.c},${r.a})`;
      fxCtx.lineWidth = 3;
      fxCtx.beginPath();
      fxCtx.arc(r.x, r.y, r.r, 0, Math.PI * 2);
      fxCtx.stroke();
    });
    if (parts.length || rings.length) requestAnimationFrame(loopFx);
    else { running = false; fxCtx.clearRect(0, 0, fxCanvas.width, fxCanvas.height); }
  };
  FX.burst = function (x, y, opts = {}) {
    if (reduce) return;
    ensureFx();
    const colors = opts.colors || ["#c8ff4d", "#3ef0e0", "#8b6cff", "#ffffff", "#ffb547"];
    const n = opts.n || 160, power = opts.power || 15;
    for (let i = 0; i < n; i++) {
      const ang = Math.random() * Math.PI * 2, sp = (0.35 + Math.random() * 0.65) * power;
      parts.push({
        x, y, vx: Math.cos(ang) * sp, vy: Math.sin(ang) * sp - power * 0.45, g: 0.32, r: Math.random() * 6, vr: (Math.random() - 0.5) * 0.35,
        w: 6 + Math.random() * 9, h: 4 + Math.random() * 6, c: colors[(Math.random() * colors.length) | 0],
        life: 1, decay: 0.006 + Math.random() * 0.008, circle: Math.random() < 0.3,
      });
    }
    rings.push({ x, y, r: 0, max: 260, a: 0.8, c: "200,255,77" }, { x, y, r: 0, max: 420, a: 0.5, c: "62,240,224" });
    if (!running) { running = true; requestAnimationFrame(loopFx); }
  };
  FX.clearFx = function () { parts = []; rings = []; };
  FX.shock = function (x, y, color = "255,92,122") {
    if (reduce) return;
    ensureFx();
    rings.push({ x, y, r: 0, max: 220, a: 0.9, c: color });
    if (!running) { running = true; requestAnimationFrame(loopFx); }
  };

  /* ------------------------------------------------------------ 故障闪屏 */
  FX.glitch = function () {
    if (reduce) return;
    document.body.classList.remove("glitching");
    void document.body.offsetWidth;
    document.body.classList.add("glitching");
    const box = document.createElement("div");
    box.className = "glitch-bars";
    for (let i = 0; i < 9; i++) {
      const b = document.createElement("div");
      const top = Math.random() * 100, hh = 2 + Math.random() * 9;
      b.style.cssText = `position:absolute;left:0;right:0;top:${top}%;height:${hh}%;background:${i % 2 ? "rgba(255,92,122,.18)" : "rgba(62,240,224,.16)"};transform:translateX(${(Math.random() - 0.5) * 60}px);mix-blend-mode:screen`;
      box.appendChild(b);
    }
    document.body.appendChild(box);
    let k = 0;
    const id = setInterval(() => {
      [...box.children].forEach((b) => { b.style.top = Math.random() * 100 + "%"; b.style.transform = `translateX(${(Math.random() - 0.5) * 80}px)`; });
      if (++k > 5) { clearInterval(id); box.remove(); }
    }, 70);
    setTimeout(() => document.body.classList.remove("glitching"), 600);
  };

  /* ------------------------------------------------------------ 长按确认按钮 */
  FX.hold = function (btn, ms, onDone) {
    let raf = 0, start = 0, p = 0;
    const fill = document.createElement("span");
    fill.className = "hold-fill";
    btn.classList.add("btn-hold");
    btn.appendChild(fill);
    const tick = (now) => {
      p = clamp((now - start) / ms, 0, 1);
      btn.style.setProperty("--p", p);
      if (p >= 1) { cancelAnimationFrame(raf); btn.style.setProperty("--p", 0); onDone(); return; }
      raf = requestAnimationFrame(tick);
    };
    const down = (e) => { e.preventDefault(); start = performance.now(); raf = requestAnimationFrame(tick); };
    const up = () => {
      cancelAnimationFrame(raf);
      const from = p;
      const t1 = performance.now();
      const back = (now) => {
        const q = clamp((now - t1) / 250, 0, 1);
        btn.style.setProperty("--p", from * (1 - q));
        if (q < 1) requestAnimationFrame(back);
      };
      requestAnimationFrame(back);
      p = 0;
    };
    btn.addEventListener("pointerdown", down);
    btn.addEventListener("pointerup", up);
    btn.addEventListener("pointerleave", up);
    btn.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onDone(); } });
  };

  /* ------------------------------------------------------------ 提示 */
  FX.toast = function (msg, type = "ok", ms = 4500) {
    const box = document.getElementById("toasts");
    if (!box) return;
    const t = document.createElement("div");
    t.className = `toast ${type}`;
    t.style.setProperty("--dur", ms + "ms");
    const ico = { ok: "✓", err: "!", warn: "!", info: "i" }[type] || "i";
    t.innerHTML = `<div class="ti">${ico}</div><div class="tm"></div><i class="tb"></i>`;
    t.querySelector(".tm").textContent = msg;
    box.appendChild(t);
    const close = () => { t.classList.add("out"); setTimeout(() => t.remove(), 500); };
    t.addEventListener("click", close);
    setTimeout(close, ms);
  };

  window.FX = FX;
})();
