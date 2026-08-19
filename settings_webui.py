"""
settings_webui.py

Страница /settings - конструктор LED-баров: режим (classic/center), метрика(и),
цвета, solid, peak hold - на каждый из 4 баров. Подключается к уже
существующему Flask-приложению вызовом register_settings_routes(app).

Никакой своей серверной логики/состояния тут нет - вся работа идёт через уже
существующие эндпойнты в shkaf_stats_bridge.py (/api/state для чтения,
/api/mode, /api/assignment(_top), /api/colors(_top), /api/solid(_top),
/api/peak для записи). Этот файл - чистая разметка + JS.
"""

from flask import Response

SETTINGS_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>shkaf-hud - настройки</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#ff8c2f">
<link rel="icon" type="image/png" href="https://raw.githubusercontent.com/RGCustom/shkaf-hud/main/favicon.png">
<style>
  * { box-sizing: border-box; }
  :root {
    --bg: #17181a; --panel: #1f2123; --border: #2c2e31;
    --text: #e6e6e6; --muted: #8a8d91; --accent: #ff8c2f; --danger: #e0483e;
  }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         margin:0; padding:24px 16px 60px; }
  .wrap { max-width:720px; margin:0 auto; }
  .brand { display:flex; align-items:center; gap:10px; margin-bottom:4px; }
  .brand .dot { width:9px; height:9px; border-radius:50%; background:var(--accent); box-shadow:0 0 8px var(--accent); }
  h1 { font-size:19px; font-weight:600; margin:0; }
  .nav { display:flex; gap:16px; margin:14px 0 24px; flex-wrap:wrap; }
  .nav a { color:var(--muted); text-decoration:none; font-size:13px; padding:6px 0; border-bottom:2px solid transparent; }
  .nav a.active { color:var(--text); border-bottom-color:var(--accent); }

  .bar-card { background:var(--panel); border:1px solid var(--border); border-radius:14px;
              padding:18px; margin-bottom:16px; }
  .bar-card h2 { font-size:13px; margin:0 0 16px; font-weight:600; display:flex; align-items:center; gap:8px; }
  .bar-card h2 .n { color:var(--muted); font-weight:400; font-size:11px; }

  .row { display:flex; align-items:center; gap:10px; margin-bottom:10px; flex-wrap:wrap; }
  .row label { font-size:12px; color:var(--muted); min-width:110px; }
  select { background:#101112; color:var(--text); border:1px solid var(--border);
           border-radius:6px; padding:6px 8px; font-size:13px; flex:1; min-width:120px; }
  input[type=color] { width:26px; height:26px; border:none; background:none; border-radius:6px; cursor:pointer; padding:0; }
  input[type=checkbox] { width:16px; height:16px; }

  .half { border-left:2px solid var(--border); padding-left:12px; margin-top:4px; margin-bottom:10px; }
  .half-title { font-size:11px; color:var(--accent); text-transform:uppercase; letter-spacing:.03em; margin-bottom:8px; }

  .colors { display:flex; gap:6px; }
  .checkbox-row { display:flex; align-items:center; gap:8px; font-size:12px; color:var(--muted); }

  .peak-row { border-top:1px solid var(--border); margin-top:12px; padding-top:12px; }

  .global-card { background:var(--panel); border:1px solid var(--border); border-radius:14px;
                 padding:18px; margin-bottom:20px; }
  .global-card h2 { font-size:11px; color:var(--muted); margin:0 0 4px; font-weight:600;
                     text-transform:uppercase; letter-spacing:.03em; }
  .global-card .hint { font-size:11px; color:var(--muted); margin-bottom:14px; line-height:1.5; }
  .slider-row { display:flex; align-items:center; gap:10px; margin-top:10px; font-size:13px; }
  .slider-row label { color:var(--muted); min-width:150px; }
  .slider-row input[type=range] { flex:1; }
  .slider-row .val { min-width:52px; text-align:right; color:var(--text); font-variant-numeric:tabular-nums; }

  footer { text-align:center; color:var(--border); font-size:11px; margin-top:20px; }
</style></head>
<body>
<div class="wrap">
  <div class="brand"><span class="dot"></span><h1>shkaf-hud</h1></div>
  <div class="nav"><a href="/">Sensors</a><a href="/settings" class="active">Settings</a><a href="/screens">OLED screens</a><a href="/flash">Flash</a></div>

  <div class="global-card">
    <h2>Peak hold — общие тайминги</h2>
    <div class="hint">Один на весь проект, а не по одному на бар — применяется ко всем барам,
      у которых Peak hold включён (см. карточки баров ниже, стиль "hold"/"fade" на каждый выбирается там же).</div>
    <div class="slider-row">
      <label>Держится (hold), сек</label>
      <input type="range" id="peak-hold-seconds" min="0" max="10" step="0.1" value="2.0">
      <span class="val" id="peak-hold-seconds-val">2.0с</span>
    </div>
    <div class="slider-row">
      <label>Затухает (fade), сек</label>
      <input type="range" id="peak-fade-seconds" min="0" max="10" step="0.1" value="1.5">
      <span class="val" id="peak-fade-seconds-val">1.5с</span>
    </div>
  </div>

  <div id="bars-container"></div>

  <footer>shkaf-hud</footer>
</div>

<script>
const BARS = ["bar0", "bar1", "bar2", "bar3"];
let metricsMap = {};
let editingPeakHold = false, editingPeakFade = false;

const peakHoldEl = document.getElementById("peak-hold-seconds");
const peakHoldValEl = document.getElementById("peak-hold-seconds-val");
peakHoldEl.addEventListener("input", () => {
  editingPeakHold = true;
  peakHoldValEl.textContent = parseFloat(peakHoldEl.value).toFixed(1) + "с";
});
peakHoldEl.addEventListener("change", () => {
  fetch("/api/peak_timing", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ hold_seconds: parseFloat(peakHoldEl.value) }) })
    .then(() => editingPeakHold = false);
});

const peakFadeEl = document.getElementById("peak-fade-seconds");
const peakFadeValEl = document.getElementById("peak-fade-seconds-val");
peakFadeEl.addEventListener("input", () => {
  editingPeakFade = true;
  peakFadeValEl.textContent = parseFloat(peakFadeEl.value).toFixed(1) + "с";
});
peakFadeEl.addEventListener("change", () => {
  fetch("/api/peak_timing", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ fade_seconds: parseFloat(peakFadeEl.value) }) })
    .then(() => editingPeakFade = false);
});

function colorRow(barId, prefix, colors, onChange) {
  // prefix: "" для низа/classic, "top" для верхней половины center-режима
  const wrap = document.createElement("div");
  wrap.className = "row";
  const label = document.createElement("label");
  label.textContent = "Цвета";
  wrap.appendChild(label);
  const colorsWrap = document.createElement("div");
  colorsWrap.className = "colors";
  ["c1", "c2", "c3"].forEach(stop => {
    const inp = document.createElement("input");
    inp.type = "color";
    inp.value = "#" + colors[stop];
    inp.addEventListener("change", () => {
      const body = {}; body[barId] = {}; body[barId][stop] = inp.value.slice(1);
      fetch(onChange, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
    });
    colorsWrap.appendChild(inp);
  });
  wrap.appendChild(colorsWrap);
  return wrap;
}

function metricSelect(barId, labelText, currentValue, endpoint) {
  const wrap = document.createElement("div");
  wrap.className = "row";
  const label = document.createElement("label");
  label.textContent = labelText;
  wrap.appendChild(label);
  const sel = document.createElement("select");
  Object.entries(metricsMap).forEach(([id, name]) => {
    const opt = document.createElement("option");
    opt.value = id; opt.textContent = name;
    if (id === currentValue) opt.selected = true;
    sel.appendChild(opt);
  });
  sel.addEventListener("change", () => {
    const body = {}; body[barId] = sel.value;
    fetch(endpoint, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
  });
  wrap.appendChild(sel);
  return wrap;
}

function solidCheckbox(barId, labelText, checked, endpoint) {
  const wrap = document.createElement("div");
  wrap.className = "checkbox-row";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = checked;
  const id = "solid-" + endpoint.replace(/\\W/g, "") + "-" + barId;
  cb.id = id;
  cb.addEventListener("change", () => {
    const body = {}; body[barId] = cb.checked;
    fetch(endpoint, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
  });
  const lab = document.createElement("label");
  lab.htmlFor = id;
  lab.textContent = labelText;
  wrap.appendChild(cb);
  wrap.appendChild(lab);
  return wrap;
}

function renderBar(barId, index, cfg) {
  const card = document.createElement("div");
  card.className = "bar-card";

  const title = document.createElement("h2");
  title.innerHTML = "BAR " + (index + 1) + ' <span class="n">' + barId + "</span>";
  card.appendChild(title);

  // --- режим ---
  const modeRow = document.createElement("div");
  modeRow.className = "row";
  const modeLabel = document.createElement("label");
  modeLabel.textContent = "Режим";
  modeRow.appendChild(modeLabel);
  const modeSel = document.createElement("select");
  [["classic", "Classic (снизу вверх)"], ["center", "Center (от центра в обе стороны)"]].forEach(([val, text]) => {
    const opt = document.createElement("option");
    opt.value = val; opt.textContent = text;
    if (val === cfg.mode[barId]) opt.selected = true;
    modeSel.appendChild(opt);
  });
  modeRow.appendChild(modeSel);
  card.appendChild(modeRow);

  // --- верх / center-блок (виден только в режиме center) - идёт ПЕРВЫМ,
  // чтобы визуально совпадать с физическим расположением на ленте (верх
  // сверху, низ снизу) ---
  const topBlock = document.createElement("div");
  topBlock.className = "half";
  topBlock.id = "top-block-" + barId;
  const topTitle = document.createElement("div");
  topTitle.className = "half-title";
  topTitle.textContent = "Верхняя половина";
  topBlock.appendChild(topTitle);
  topBlock.appendChild(metricSelect(barId, "Метрика", cfg.assignment_top[barId], "/api/assignment_top"));
  topBlock.appendChild(colorRow(barId, "top", cfg.colors_top[barId], "/api/colors_top"));
  topBlock.appendChild(solidCheckbox(barId, "Цвет на 100%", cfg.solid_top[barId], "/api/solid_top"));
  card.appendChild(topBlock);

  // --- низ / classic-блок (всегда виден) ---
  const bottomBlock = document.createElement("div");
  const bottomTitle = document.createElement("div");
  bottomTitle.className = "half-title";
  bottomTitle.id = "bottom-title-" + barId;
  bottomBlock.appendChild(bottomTitle);
  bottomBlock.appendChild(metricSelect(barId, "Метрика", cfg.assignment[barId], "/api/assignment"));
  bottomBlock.appendChild(colorRow(barId, "", cfg.colors[barId], "/api/colors"));
  bottomBlock.appendChild(solidCheckbox(barId, "Цвет на 100%", cfg.solid[barId], "/api/solid"));
  card.appendChild(bottomBlock);

  function applyModeVisibility(mode) {
    bottomTitle.textContent = mode === "center" ? "Нижняя половина" : "";
    topBlock.style.display = mode === "center" ? "block" : "none";
  }
  applyModeVisibility(cfg.mode[barId]);

  modeSel.addEventListener("change", () => {
    applyModeVisibility(modeSel.value);
    const body = {}; body[barId] = modeSel.value;
    fetch("/api/mode", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
  });

  // --- peak hold (независимо от режима) ---
  const peakRow = document.createElement("div");
  peakRow.className = "peak-row";

  const peakToggle = document.createElement("div");
  peakToggle.className = "checkbox-row";
  const peakCb = document.createElement("input");
  peakCb.type = "checkbox";
  peakCb.checked = cfg.peak[barId].enabled;
  const peakId = "peak-" + barId;
  peakCb.id = peakId;
  const peakLab = document.createElement("label");
  peakLab.htmlFor = peakId;
  peakLab.textContent = "Peak hold (точка недавнего максимума)";
  peakToggle.appendChild(peakCb);
  peakToggle.appendChild(peakLab);
  peakRow.appendChild(peakToggle);

  const styleRow = document.createElement("div");
  styleRow.className = "row";
  const styleLabel = document.createElement("label");
  styleLabel.textContent = "Стиль";
  styleRow.appendChild(styleLabel);
  const styleSel = document.createElement("select");
  [["hold", "Держится и гаснет"], ["fade", "Плавно затухает"]].forEach(([val, text]) => {
    const opt = document.createElement("option");
    opt.value = val; opt.textContent = text;
    if (val === cfg.peak[barId].style) opt.selected = true;
    styleSel.appendChild(opt);
  });
  styleRow.appendChild(styleSel);
  peakRow.appendChild(styleRow);
  card.appendChild(peakRow);

  function sendPeak(partial) {
    const body = {}; body[barId] = partial;
    fetch("/api/peak", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
  }
  peakCb.addEventListener("change", () => sendPeak({ enabled: peakCb.checked }));
  styleSel.addEventListener("change", () => sendPeak({ style: styleSel.value }));

  return card;
}

function render(state) {
  metricsMap = state.metrics;

  if (!editingPeakHold) {
    peakHoldEl.value = state.cfg.peak_hold_seconds;
    peakHoldValEl.textContent = parseFloat(state.cfg.peak_hold_seconds).toFixed(1) + "с";
  }
  if (!editingPeakFade) {
    peakFadeEl.value = state.cfg.peak_fade_seconds;
    peakFadeValEl.textContent = parseFloat(state.cfg.peak_fade_seconds).toFixed(1) + "с";
  }

  const container = document.getElementById("bars-container");
  container.innerHTML = "";
  BARS.forEach((barId, i) => container.appendChild(renderBar(barId, i, state.cfg)));
}

fetch("/api/state").then(r => r.json()).then(render);

if ('serviceWorker' in navigator) { navigator.serviceWorker.register('/sw.js').catch(() => {}); }
</script>
</body></html>
"""


def register_settings_routes(app):
    @app.route("/settings")
    def settings_page():
        return Response(SETTINGS_PAGE_HTML, mimetype="text/html")
