"use strict";

const $ = (id) => document.getElementById(id);
const COLORS = ["#58a6ff", "#f78166", "#3fb950", "#d2a8ff", "#ffa657", "#79c0ff", "#ff7b72", "#7ee787", "#e3b341", "#bc8cff"];
const VIRIDIS = [[68,1,84],[72,40,120],[62,74,137],[49,104,142],[38,130,142],[31,158,137],[53,183,121],[109,205,89],[180,222,44],[253,231,37]];

let manifest = {series: [], grids: [], texts: [], title: ""};
let artifacts = [];
let selected = -1;
let mode = "series";
let visible = [];
let view = null;
let baseView = null;
let gl = null, lineProg = null, gridProg = null, tex = null;
let grid = null;
let frameIndex = 0, frameTimer = null;
let dragging = null;
let canvasW = 0, canvasH = 0;

const glCanvas = $("gl");
const overlay = $("overlay");
const wrap = $("plotWrap");
const ctx = overlay.getContext("2d");

function resize() {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = Math.max(1, Math.floor(wrap.clientWidth * dpr));
  const h = Math.max(1, Math.floor(wrap.clientHeight * dpr));
  if (glCanvas.width !== w || glCanvas.height !== h) {
    glCanvas.width = w; glCanvas.height = h;
    overlay.width = w; overlay.height = h;
    canvasW = w; canvasH = h;
    if (gl) gl.viewport(0, 0, w, h);
    draw();
  } else {
    canvasW = w; canvasH = h;
  }
}

function compile(gl, type, src) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src); gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
    throw new Error(gl.getShaderInfoLog(s) || "shader compile failed");
  }
  return s;
}

function program(gl, vs, fs) {
  const p = gl.createProgram();
  gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, vs));
  gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
    throw new Error(gl.getProgramInfoLog(p) || "program link failed");
  }
  return p;
}

function initGL() {
  gl = glCanvas.getContext("webgl", {antialias: true, alpha: false}) ||
       glCanvas.getContext("experimental-webgl");
  if (!gl) {
    $("status").textContent = "WebGL unavailable; use a WebGL-capable browser";
    return;
  }
  lineProg = program(gl,
    "attribute vec2 a_pos; uniform vec4 u_bounds; uniform vec4 u_frame; void main(){ vec2 t=(a_pos-u_bounds.xy)/(u_bounds.zw-u_bounds.xy); vec2 p=u_frame.xy + t*u_frame.zw; gl_Position=vec4(p,0.0,1.0); gl_PointSize=4.0; }",
    "precision mediump float; uniform vec3 u_color; void main(){ gl_FragColor=vec4(u_color,1.0); }");
  gridProg = program(gl,
    "attribute vec2 a_pos; varying vec2 v_uv; void main(){ v_uv=vec2(a_pos.x*0.5+0.5, 0.5-a_pos.y*0.5); gl_Position=vec4(a_pos,0.0,1.0); }",
    "precision mediump float; varying vec2 v_uv; uniform sampler2D u_tex; void main(){ gl_FragColor=texture2D(u_tex,v_uv); }");
}

function fbounds(items) {
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const s of items) {
    if (!s.visible) continue;
    for (let i = 0; i < s.x.length; i++) {
      const x = +s.x[i], y = +s.y[i];
      if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
      x0 = Math.min(x0, x); x1 = Math.max(x1, x);
      y0 = Math.min(y0, y); y1 = Math.max(y1, y);
    }
  }
  if (!Number.isFinite(x0)) return {x0: 0, x1: 1, y0: 0, y1: 1};
  let dx = x1 - x0, dy = y1 - y0;
  if (dx === 0) { dx = Math.abs(x0) || 1; x0 -= dx * 0.5; x1 += dx * 0.5; }
  if (dy === 0) { dy = Math.abs(y0) || 1; y0 -= dy * 0.5; y1 += dy * 0.5; }
  const px = dx * 0.04, py = dy * 0.06;
  return {x0: x0 - px, x1: x1 + px, y0: y0 - py, y1: y1 + py};
}

function resetView() {
  baseView = fbounds(manifest.series);
  view = {...baseView};
  draw();
}

function niceTicks(lo, hi, n) {
  if (!(Number.isFinite(lo) && Number.isFinite(hi)) || hi <= lo) return [lo];
  const raw = (hi - lo) / Math.max(1, n);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || 10 * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(v);
  return out;
}

function fmt(v) {
  if (!Number.isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a !== 0 && (a >= 1e5 || a < 1e-3)) return v.toExponential(2);
  return (Math.round(v * 1e6) / 1e6).toString();
}

function drawAxes() {
  const w = canvasW, h = canvasH, dpr = w / Math.max(1, wrap.clientWidth);
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (!view) return;
  const L = 64 * dpr, R = 20 * dpr, T = 20 * dpr, B = 38 * dpr;
  const x0 = view.x0, x1 = view.x1, y0 = view.y0, y1 = view.y1;
  const mx = (x) => L + (x - x0) / (x1 - x0) * (w - L - R);
  const my = (y) => h - B - (y - y0) / (y1 - y0) * (h - T - B);
  ctx.strokeStyle = "#30363d"; ctx.lineWidth = 1 * dpr;
  ctx.fillStyle = "#8b949e";
  ctx.font = `${11 * dpr}px "Segoe UI", system-ui, sans-serif`;
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  for (const t of niceTicks(x0, x1, 8)) {
    const x = mx(t); if (x < L || x > w - R) continue;
    ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, h - B); ctx.stroke();
    ctx.fillText(fmt(t), x, h - B + 6 * dpr);
  }
  ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (const t of niceTicks(y0, y1, 7)) {
    const y = my(t); if (y < T || y > h - B) continue;
    ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(w - R, y); ctx.stroke();
    ctx.fillText(fmt(t), L - 7 * dpr, y);
  }
  ctx.strokeStyle = "#484f58";
  ctx.beginPath(); ctx.moveTo(L, T); ctx.lineTo(L, h - B); ctx.lineTo(w - R, h - B); ctx.stroke();
  if (y0 <= 0 && y1 >= 0) {
    ctx.strokeStyle = "#6e7681"; ctx.beginPath(); ctx.moveTo(L, my(0)); ctx.lineTo(w - R, my(0)); ctx.stroke();
  }
  if (x0 <= 0 && x1 >= 0) {
    ctx.strokeStyle = "#6e7681"; ctx.beginPath(); ctx.moveTo(mx(0), T); ctx.lineTo(mx(0), h - B); ctx.stroke();
  }
}

function lineColor(i) { return COLORS[i % COLORS.length]; }
function hexRgb(h) { return [parseInt(h.slice(1,3),16)/255, parseInt(h.slice(3,5),16)/255, parseInt(h.slice(5,7),16)/255]; }

function drawSeries() {
  if (!gl || !lineProg || !view) return;
  gl.useProgram(lineProg);
  gl.clearColor(0.055, 0.067, 0.09, 1); gl.clear(gl.COLOR_BUFFER_BIT);
  // Keep axes thickness in the same normalized space as overlay: just map full canvas for curves.
  const uB = gl.getUniformLocation(lineProg, "u_bounds");
  gl.uniform4f(uB, view.x0, view.y0, view.x1, view.y1);
  const w = Math.max(1, wrap.clientWidth), h = Math.max(1, wrap.clientHeight);
  const dpr = Math.max(1, canvasW / w);
  const L = 64 * dpr, R = 20 * dpr, T = 20 * dpr, B = 38 * dpr;
  // clip-space rectangle matching the 2D axes margins
  gl.uniform4f(gl.getUniformLocation(lineProg, "u_frame"),
               -1.0 + 2.0 * L / canvasW, -1.0 + 2.0 * B / canvasH,
               2.0 * (canvasW - L - R) / canvasW,
               2.0 * (canvasH - T - B) / canvasH);
  const aPos = gl.getAttribLocation(lineProg, "a_pos");
  const uColor = gl.getUniformLocation(lineProg, "u_color");
  gl.enableVertexAttribArray(aPos);
  let idx = 0;
  for (const s of manifest.series) {
    const color = hexRgb(lineColor(s.colorIndex ?? idx));
    if (s.visible) {
      const pts = [];
      for (let i = 0; i < s.x.length; i++) {
        const x = +s.x[i], y = +s.y[i];
        if (Number.isFinite(x) && Number.isFinite(y)) pts.push(x, y);
      }
      if (pts.length >= 4) {
        const buf = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, buf);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(pts), gl.STATIC_DRAW);
        gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);
        gl.uniform3f(uColor, color[0], color[1], color[2]);
        if (s.kind === "points") {
          gl.drawArrays(gl.POINTS, 0, pts.length / 2);
        } else if (s.kind === "bars") {
          const bars = [];
          for (let i = 0; i < pts.length; i += 2) bars.push(pts[i], 0, pts[i], pts[i+1]);
          const bb = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, bb);
          gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(bars), gl.STATIC_DRAW);
          gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);
          gl.drawArrays(gl.LINES, 0, bars.length / 2);
          gl.deleteBuffer(bb);
        } else {
          gl.drawArrays(gl.LINE_STRIP, 0, pts.length / 2);
        }
        gl.deleteBuffer(buf);
      }
    }
    idx++;
  }
  drawAxes();
  $("status").textContent = `${manifest.series.length} series · x [${fmt(view.x0)}, ${fmt(view.x1)}] · y [${fmt(view.y0)}, ${fmt(view.y1)}]`;
}

function colorFor(v, lo, hi) {
  const t = Math.max(0, Math.min(1, (v - lo) / (hi - lo || 1)));
  const p = t * (VIRIDIS.length - 1), i = Math.floor(p), f = p - i;
  const a = VIRIDIS[i], b = VIRIDIS[Math.min(i + 1, VIRIDIS.length - 1)];
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
}

async function uploadGrid() {
  const g = grid;
  if (!g) return;
  if (g.lazy) {
    try { await ensureGridFrame(g, frameIndex); }
    catch (e) { $("status").textContent = String(e.message || e); return; }
  }
  if (!g.frames || !g.frames.length) return;
  const frame = g.frames[frameIndex] || g.frames[0];
  const w = g.cols, h = g.rows;
  const rgba = new Uint8Array(w * h * 4);
  for (let i = 0; i < w * h; i++) {
    const c = colorFor(frame[i], g.lo, g.hi);
    rgba[i*4] = c[0]; rgba[i*4+1] = c[1]; rgba[i*4+2] = c[2]; rgba[i*4+3] = 255;
  }
  tex = tex || gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  $("frameLabel").textContent = `${frameIndex + 1} / ${g.frameCount || g.frames.length}`;
  $("frameSlider").max = String(Math.max(0, (g.frameCount || g.frames.length) - 1));
  $("frameSlider").value = String(frameIndex);
  $("status").textContent = `${g.name} · ${g.cols}×${g.rows} · ${g.frameCount || g.frames.length} frame(s)` +
    (g.lazy ? " · streaming frames" : ` · range [${fmt(g.lo)}, ${fmt(g.hi)}]`);
}

function drawGrid() {
  if (!gl || !gridProg || !grid) return;
  gl.useProgram(gridProg);
  gl.clearColor(0, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT);
  if (!tex) return;
  const a = gl.getAttribLocation(gridProg, "a_pos");
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, -1,1, 1,-1, 1,1]), gl.STATIC_DRAW);
  gl.enableVertexAttribArray(a); gl.vertexAttribPointer(a, 2, gl.FLOAT, false, 0, 0);
  gl.activeTexture(gl.TEXTURE0);
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.uniform1i(gl.getUniformLocation(gridProg, "u_tex"), 0);
  gl.drawArrays(gl.TRIANGLES, 0, 6);
  gl.deleteBuffer(buf);
  ctx.setTransform(1,0,0,1,0,0); ctx.clearRect(0,0,canvasW,canvasH);
}

function draw() {
  if (!gl) return;
  if (mode === "grid") drawGrid(); else drawSeries();
}

async function loadGridFrames(g) {
  if (g.frames || g.values) {
    if (!g.frames) g.frames = [Float32Array.from(g.values || [])];
    g.frameCount = g.frames.length;
    if (!Number.isFinite(g.lo)) {
      let lo = Infinity, hi = -Infinity;
      for (const f of g.frames) for (const v of f) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
      g.lo = Number.isFinite(lo) ? lo : 0; g.hi = Number.isFinite(hi) ? hi : 1;
    }
    return;
  }
  if (!g.framesUrl) {
    g.frames = [new Float32Array(0)];
    g.frameCount = 1;
    return;
  }
  const frameBytes = g.frameBytes || (g.rows * g.cols * 4);
  // Small sequences are cheap to fetch once; large ones are fetched per frame.
  if (frameBytes * (g.frameCount || 1) <= 64 * 1024 * 1024) {
    const res = await fetch(g.framesUrl);
    if (!res.ok) throw new Error(`cannot load ${g.framesUrl}`);
    const buf = await res.arrayBuffer();
    const dv = new DataView(buf);
    const cols = dv.getUint32(0, true), rows = dv.getUint32(4, true), count = dv.getUint32(8, true);
    let p = 12, frames = [], lo = Infinity, hi = -Infinity;
    const cell = cols * rows;
    for (let f = 0; f < count; f++) {
      if (p + cell * 4 > buf.byteLength) break;
      const a = new Float32Array(buf.slice(p, p + cell * 4));
      p += cell * 4;
      for (let i = 0; i < a.length; i++) { lo = Math.min(lo, a[i]); hi = Math.max(hi, a[i]); }
      frames.push(a);
    }
    g.rows = rows; g.cols = cols; g.frames = frames; g.frameCount = frames.length;
    g.lo = Number.isFinite(lo) ? lo : 0; g.hi = Number.isFinite(hi) ? hi : 1;
    return;
  }
  // Lazy mode: only frame 0 is fetched now.
  g.frames = [];
  g.lazy = true;
  g.frameCount = g.frameCount || 1;
  g.lo = Infinity; g.hi = -Infinity;
  await ensureGridFrame(g, 0);
}

async function ensureGridFrame(g, index) {
  if (g.frames && g.frames[index]) return g.frames[index];
  if (!g.framesUrl) return null;
  const fb = g.frameBytes || (g.rows * g.cols * 4);
  const start = 12 + index * fb;
  const res = await fetch(g.framesUrl, {headers: {Range: `bytes=${start}-${start + fb - 1}`}});
  if (!res.ok && res.status !== 206) throw new Error(`cannot load frame ${index}`);
  let buf = await res.arrayBuffer();
  if (res.status === 200) buf = buf.slice(start, start + fb);
  const a = new Float32Array(buf);
  g.frames = g.frames || []; g.frames[index] = a;
  let lo = g.lo, hi = g.hi;
  for (let i = 0; i < a.length; i++) { lo = Math.min(lo, a[i]); hi = Math.max(hi, a[i]); }
  g.lo = Number.isFinite(lo) ? lo : 0; g.hi = Number.isFinite(hi) ? hi : 1;
  return a;
}

function showArtifact(i) {
  selected = i;
  const a = artifacts[i];
  document.querySelectorAll(".artifact").forEach((el, k) => el.classList.toggle("selected", k === i));
  stopPlay();
  mode = a.kind === "grid" ? "grid" : "series";
  $("gridControls").classList.toggle("visible", mode === "grid");
  if (mode === "grid") {
    grid = a.grid; frameIndex = 0; tex = null;
    loadGridFrames(grid).then(() => uploadGrid()).then(() => draw())
      .catch(err => { $("status").textContent = String(err.message || err); });
  } else {
    resetView(); draw();
  }
}

function buildSidebar() {
  const box = $("artifacts"); box.innerHTML = "";
  artifacts = [];
  manifest.series.forEach((s, i) => {
    s.visible = true; s.colorIndex = i;
    artifacts.push({kind: "series", label: s.name || `series ${i}`, grid: null});
  });
  (manifest.grids || []).forEach((g, i) => {
    artifacts.push({kind: "grid", label: g.name || `grid ${i}`, grid: g});
  });
  artifacts.forEach((a, i) => {
    const b = document.createElement("button");
    b.className = "artifact";
    b.innerHTML = `${escapeHtml(a.label)}<span class="kind">${a.kind}</span>`;
    b.onclick = () => showArtifact(i);
    box.appendChild(b);
  });
  buildLegend();
  if (artifacts.length) showArtifact(0);
  else $("status").textContent = "no artifacts found";
}

function buildLegend() {
  const box = $("legend"); box.innerHTML = "";
  manifest.series.forEach((s, i) => {
    const lab = document.createElement("label");
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = true;
    cb.onchange = () => { s.visible = cb.checked; if (mode === "series") { resetViewIfNeeded(); draw(); } };
    const sw = document.createElement("span"); sw.className = "swatch"; sw.style.background = lineColor(s.colorIndex ?? i);
    lab.append(cb, sw, document.createTextNode(s.name || `series ${i}`));
    box.appendChild(lab);
  });
}

function resetViewIfNeeded() { if (!view) resetView(); }

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

function stopPlay() {
  if (frameTimer) { clearInterval(frameTimer); frameTimer = null; $("play").textContent = "Play"; }
}

function bindInteraction() {
  wrap.addEventListener("wheel", (e) => {
    if (mode !== "series" || !view) return;
    e.preventDefault();
    const rect = wrap.getBoundingClientRect();
    const fx = (e.clientX - rect.left) / Math.max(1, rect.width);
    const fy = 1 - (e.clientY - rect.top) / Math.max(1, rect.height);
    const k = Math.exp(e.deltaY * 0.0012);
    const x = view.x0 + (view.x1 - view.x0) * fx;
    const y = view.y0 + (view.y1 - view.y0) * fy;
    view.x0 = x + (view.x0 - x) * k; view.x1 = x + (view.x1 - x) * k;
    view.y0 = y + (view.y0 - y) * k; view.y1 = y + (view.y1 - y) * k;
    draw();
  }, {passive: false});
  let last = null;
  wrap.addEventListener("mousedown", (e) => {
    if (mode !== "series" || !view) return;
    last = {x: e.clientX, y: e.clientY, v: {...view}};
  });
  window.addEventListener("mousemove", (e) => {
    if (!last || !view) return;
    const rect = wrap.getBoundingClientRect();
    const dx = (e.clientX - last.x) / Math.max(1, rect.width) * (last.v.x1 - last.v.x0);
    const dy = (e.clientY - last.y) / Math.max(1, rect.height) * (last.v.y1 - last.v.y0);
    view = {x0: last.v.x0 - dx, x1: last.v.x1 - dx, y0: last.v.y0 + dy, y1: last.v.y1 + dy};
    draw();
  });
  window.addEventListener("mouseup", () => { last = null; });
  wrap.addEventListener("dblclick", () => { if (mode === "series") resetView(); });
  $("play").onclick = () => {
    if (frameTimer) { stopPlay(); return; }
    if (!grid || (grid.frameCount || 0) < 2) return;
    $("play").textContent = "Pause";
    frameTimer = setInterval(() => {
      frameIndex = (frameIndex + 1) % (grid.frameCount || grid.frames.length);
      uploadGrid().then(() => draw());
    }, 100);
  };
  $("frameSlider").oninput = (e) => {
    if (!grid) return;
    frameIndex = +e.target.value;
    uploadGrid().then(() => draw());
  };
  window.addEventListener("resize", resize);
  const drop = $("drop");
  drop.onclick = () => $("fileInput").click();
  $("fileInput").onchange = (e) => loadFiles([...e.target.files]);
  ["dragenter","dragover"].forEach(k => drop.addEventListener(k, (e) => { e.preventDefault(); drop.classList.add("hot"); }));
  ["dragleave","drop"].forEach(k => drop.addEventListener(k, () => drop.classList.remove("hot")));
  drop.addEventListener("drop", (e) => { e.preventDefault(); loadFiles([...e.dataTransfer.files]); });
}

function parseCsv(text, name) {
  const rows = text.trim().split(/\r?\n/).map(r => r.split(","));
  if (!rows.length) return null;
  const hdr = rows[0].map(x => x.trim().toLowerCase());
  const out = [];
  if (hdr[0] === "x" && hdr[1] === "y") {
    const x = [], y = [];
    for (let i = 1; i < rows.length; i++) {
      const a = +rows[i][0], b = +rows[i][1];
      if (Number.isFinite(a) && Number.isFinite(b)) { x.push(a); y.push(b); }
    }
    out.push({name, kind: "line", x, y});
  } else {
    const x = [], cols = [];
    for (let c = 1; c < hdr.length; c++) cols.push([]);
    for (let i = 0; i < rows.length; i++) {
      const a = +rows[i][0];
      if (!Number.isFinite(a)) continue;
      x.push(a);
      for (let c = 1; c < hdr.length; c++) {
        const b = +rows[i][c]; cols[c-1].push(Number.isFinite(b) ? b : NaN);
      }
    }
    for (let c = 0; c < cols.length; c++) out.push({name: hdr[c+1] || `${name}:${c}`, kind: "line", x, y: cols[c]});
  }
  return out;
}

function loadFiles(files) {
  if (!files || !files.length) return;
  let pending = files.length;
  const add = (series) => {
    if (series) for (const s of series) {
      s.visible = true; s.colorIndex = manifest.series.length;
      manifest.series.push(s);
    }
    pending--;
    if (!pending) { selected = -1; view = null; buildSidebar(); }
  };
  for (const f of files) {
    const ext = (f.name.split(".").pop() || "").toLowerCase();
    const r = new FileReader();
    r.onload = () => {
      try {
        if (ext === "json") {
          const obj = JSON.parse(String(r.result));
          if (obj.series || obj.grids) {
            manifest.series.push(...(obj.series || []));
            manifest.grids = (manifest.grids || []).concat(obj.grids || []);
            add([]);
          } else add([]);
        } else if (ext === "csv") {
          add(parseCsv(String(r.result), f.name.replace(/\.csv$/i, "")));
        } else {
          const nums = String(r.result).split(/\s+/).map(Number).filter(Number.isFinite);
          add([{name: f.name, kind: "line", x: nums.map((_, i) => i), y: nums}]);
        }
      } catch (e) { add([]); }
    };
    r.readAsText(f);
  }
}

async function boot() {
  initGL();
  bindInteraction();
  resize();
  const params = new URLSearchParams(location.search);
  const url = params.get("data") || "/manifest.json";
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(String(res.status));
    manifest = await res.json();
    $("subtitle").textContent = manifest.title || "";
    buildSidebar();
  } catch (e) {
    $("subtitle").textContent = "no manifest loaded — drop CSV/TXT/JSON";
    $("status").textContent = "";
  }
  resize();
}

boot();
