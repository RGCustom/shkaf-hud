"""
screens_webui.py

Отдельная страница /screens - редактор OLED-экранов. Подключается к уже
существующему Flask-приложению вызовом register_screens_routes(app, get_context).

get_context - функция без аргументов, возвращающая текущий context (тот же
словарь, что build_active_screens ожидает) - нужна для живого превью при
редактировании шаблона.
"""

import json
import threading

from flask import request, jsonify, Response

import screens
import templates
import variables

_lock = threading.Lock()
_screens = screens.load_screens()


# ---------------- HTML / CSS / JS (Всё в одном файле) ----------------

SCREENS_PAGE_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>shkaf-hud - Screens</title>
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#ff8c2f">
    <script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.0/Sortable.min.js"></script>
    <style>
        :root { --bg: #17181a; --card: #2a2b2e; --accent: #ff8c2f; --text: #e0e0e0; --muted: #888; }
        body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0; }
        header { background: #222; padding: 12px 20px; display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid var(--accent); }
        header h1 { margin: 0; font-size: 1.2em; color: var(--accent); }
        nav a { color: var(--text); text-decoration: none; margin-left: 15px; font-size: 0.95em; }
        nav a.active { color: var(--accent); font-weight: bold; }
        
        .container { display: flex; flex-wrap: wrap; padding: 20px; gap: 20px; max-width: 1200px; margin: 0 auto; }
        .col { flex: 1; min-width: 320px; }
        .col-left { flex: 1.2; }
        
        /* Список экранов */
        .screen-list { list-style: none; padding: 0; margin: 0; }
        .screen-item { background: var(--card); margin-bottom: 8px; padding: 12px; border-radius: 6px; cursor: grab; display: flex; justify-content: space-between; align-items: center; border-left: 4px solid transparent; transition: background 0.2s; }
        .screen-item:hover { background: #333438; }
        .screen-item.active { border-left-color: var(--accent); background: #3a3b3e; }
        .screen-item .info { flex-grow: 1; overflow: hidden; }
        .screen-item .name { font-weight: bold; font-size: 1.05em; }
        .screen-item .meta { font-size: 0.8em; color: var(--muted); margin-top: 2px; }
        .screen-item .lines { font-family: monospace; font-size: 0.75em; color: #666; margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .drag-handle { padding: 0 10px; color: #555; font-size: 1.2em; cursor: grab; }
        
        /* Форма */
        .form-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 6px; font-weight: bold; font-size: 0.85em; color: #aaa; text-transform: uppercase; letter-spacing: 0.5px; }
        input, textarea { width: 100%; background: #111; color: #fff; border: 1px solid #444; border-radius: 4px; padding: 10px; box-sizing: border-box; font-family: monospace; font-size: 0.95em; }
        input:focus, textarea:focus { outline: none; border-color: var(--accent); }
        textarea { resize: vertical; min-height: 50px; }
        
        .btn-row { display: flex; gap: 10px; margin-top: 20px; flex-wrap: wrap; }
        button { background: var(--accent); color: #000; border: none; padding: 10px 16px; border-radius: 4px; cursor: pointer; font-weight: bold; font-size: 0.9em; transition: opacity 0.2s; }
        button:hover { opacity: 0.85; }
        button.secondary { background: #444; color: #fff; }
        button.danger { background: #d32f2f; color: #fff; }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        
        /* Легенда переменных */
        .legend-group { margin-bottom: 15px; }
        .legend-title { font-weight: bold; color: var(--accent); margin-bottom: 6px; font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.5px; }
        .legend-items { display: flex; flex-wrap: wrap; gap: 6px; }
        .var-chip { background: var(--card); border: 1px solid #444; padding: 4px 8px; border-radius: 4px; font-size: 0.8em; cursor: pointer; font-family: monospace; transition: all 0.2s; }
        .var-chip:hover { background: #3a3b3e; border-color: var(--accent); color: var(--accent); }
        
        /* Превью */
        .preview-box { background: #000; color: #0f0; font-family: monospace; padding: 12px; border-radius: 4px; min-height: 60px; margin-top: 10px; white-space: pre-wrap; font-size: 0.9em; border: 1px solid #333; }
        .preview-error { color: #ff5252; }
        
        @media (max-width: 768px) {
            .container { flex-direction: column; }
        }
    </style>
</head>
<body>
    <header>
        <h1>shkaf-hud</h1>
        <nav>
            <a href="/">Sensors</a>
            <a href="/screens" class="active">OLED screens</a>
        </nav>
    </header>

    <div class="container">
        <!-- Левая колонка: Список экранов -->
        <div class="col col-left">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                <h2 style="margin: 0; font-size: 1.1em;">Экраны (порядок ротации)</h2>
                <button onclick="addNewScreen()">+ Добавить</button>
            </div>
            <ul id="screen-list" class="screen-list">
                <!-- Заполняется JS -->
            </ul>
        </div>

        <!-- Правая колонка: Редактор и Легенда -->
        <div class="col">
            <div id="editor-panel" style="display: none;">
                <h2 style="margin-top: 0; font-size: 1.1em;">Редактирование экрана</h2>
                
                <div class="form-group">
                    <label>Название (только для интерфейса)</label>
                    <input type="text" id="inp-name">
                </div>
                
                <div class="form-group">
                    <label>Время показа, сек</label>
                    <input type="number" id="inp-duration" min="1" step="0.5">
                </div>

                <div class="form-group">
                    <label>Строка 1 (L1)</label>
                    <textarea id="inp-l1" onfocus="activeTextarea=this"></textarea>
                </div>
                <div class="form-group">
                    <label>Строка 2 (L2)</label>
                    <textarea id="inp-l2" onfocus="activeTextarea=this"></textarea>
                </div>
                <div class="form-group">
                    <label>Строка 3 (L3)</label>
                    <textarea id="inp-l3" onfocus="activeTextarea=this"></textarea>
                </div>

                <div class="btn-row">
                    <button onclick="saveCurrentScreen()">Сохранить</button>
                    <button class="secondary" onclick="runPreview()">Live Preview</button>
                    <button class="danger" onclick="deleteCurrentScreen()" style="margin-left: auto;">Удалить</button>
                </div>

                <div style="margin-top: 20px;">
                    <label>Результат превью:</label>
                    <div id="preview-output" class="preview-box">Нажмите "Live Preview" для проверки...</div>
                </div>
            </div>
            
            <div id="empty-state" style="text-align: center; color: var(--muted); margin-top: 50px;">
                Выберите экран из списка или создайте новый
            </div>

            <h2 style="margin-top: 30px; font-size: 1.1em;">Доступные переменные</h2>
            <p style="font-size: 0.85em; color: var(--muted); margin-top: -10px;">Кликните, чтобы вставить в активное поле</p>
            <div id="variables-legend">
                <!-- Заполняется JS -->
            </div>
        </div>
    </div>

    <script>
        let screens = [];
        let variables = [];
        let currentScreenId = null;
        let activeTextarea = null;

        // --- Инициализация ---
        async function init() {
            const [sRes, vRes] = await Promise.all([
                fetch('/api/screens').then(r => r.json()),
                fetch('/api/variables').then(r => r.json())
            ]);
            screens = sRes;
            variables = vRes;
            renderScreenList();
            renderVariablesLegend();
            initSortable();
        }

        // --- Рендер списка экранов ---
        function renderScreenList() {
            const list = document.getElementById('screen-list');
            list.innerHTML = '';
            screens.forEach(s => {
                const li = document.createElement('li');
                li.className = `screen-item ${s.id === currentScreenId ? 'active' : ''}`;
                li.dataset.id = s.id;
                li.innerHTML = `
                    <div class="drag-handle">☰</div>
                    <div class="info" onclick="selectScreen('${s.id}')">
                        <div class="name">${escapeHtml(s.name)}</div>
                        <div class="meta">${s.duration} сек | ${s.enabled ? 'Вкл' : 'Выкл'}</div>
                        <div class="lines">L1: ${escapeHtml(s.l1 || '-')}</div>
                        <div class="lines">L2: ${escapeHtml(s.l2 || '-')}</div>
                        <div class="lines">L3: ${escapeHtml(s.l3 || '-')}</div>
                    </div>
                `;
                list.appendChild(li);
            });
        }

        // --- Рендер легенды переменных ---
        function renderVariablesLegend() {
            const container = document.getElementById('variables-legend');
            const groups = {};
            variables.forEach(v => {
                if (!groups[v.group]) groups[v.group] = [];
                groups[v.group].push(v);
            });

            const groupNames = {
                'scalar': 'Система / Сеть / Plex',
                'stream': 'Активные стримы',
                'recent': 'Недавно добавленные',
                'qbt': 'qBittorrent'
            };

            let html = '';
            for (const [group, items] of Object.entries(groups)) {
                html += `<div class="legend-group">
                    <div class="legend-title">${groupNames[group] || group}</div>
                    <div class="legend-items">`;
                items.forEach(item => {
                    html += `<div class="var-chip" onclick="insertVar('{${item.name}}')" title="${escapeHtml(item.label)}">{${item.name}}</div>`;
                });
                html += `</div></div>`;
            }
            container.innerHTML = html;
        }

        // --- Drag and Drop ---
        function initSortable() {
            new Sortable(document.getElementById('screen-list'), {
                handle: '.drag-handle',
                animation: 150,
                onEnd: async (evt) => {
                    const newOrder = Array.from(evt.to.children).map(el => el.dataset.id);
                    await fetch('/api/screens/reorder', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({order: newOrder})
                    });
                    // Обновляем локальный массив screens в соответствии с новым порядком
                    const byId = Object.fromEntries(screens.map(s => [s.id, s]));
                    screens = newOrder.map(id => byId[id]).filter(Boolean);
                }
            });
        }

        // --- Редактирование ---
        function selectScreen(id) {
            currentScreenId = id;
            const s = screens.find(x => x.id === id);
            if (!s) return;

            document.getElementById('editor-panel').style.display = 'block';
            document.getElementById('empty-state').style.display = 'none';
            document.getElementById('inp-name').value = s.name;
            document.getElementById('inp-duration').value = s.duration;
            document.getElementById('inp-l1').value = s.l1 || '';
            document.getElementById('inp-l2').value = s.l2 || '';
            document.getElementById('inp-l3').value = s.l3 || '';
            
            renderScreenList(); // Обновить подсветку
            document.getElementById('preview-output').textContent = 'Нажмите "Live Preview" для проверки...';
            document.getElementById('preview-output').className = 'preview-box';
        }

        function addNewScreen() {
            fetch('/api/screens', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: 'New Screen', l1: '', l2: '', l3: '', duration: 4.0})
            }).then(r => r.json()).then(s => {
                screens.push(s);
                renderScreenList();
                selectScreen(s.id);
            });
        }

        async function saveCurrentScreen() {
            if (!currentScreenId) return;
            const data = {
                name: document.getElementById('inp-name').value,
                duration: parseFloat(document.getElementById('inp-duration').value) || 4.0,
                l1: document.getElementById('inp-l1').value,
                l2: document.getElementById('inp-l2').value,
                l3: document.getElementById('inp-l3').value
            };
            
            await fetch(`/api/screens/${currentScreenId}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
            
            // Обновляем локально
            const idx = screens.findIndex(s => s.id === currentScreenId);
            if (idx !== -1) {
                screens[idx] = {...screens[idx], ...data};
                renderScreenList();
            }
        }

        async function deleteCurrentScreen() {
            if (!currentScreenId) return;
            if (!confirm('Удалить этот экран?')) return;
            
            await fetch(`/api/screens/${currentScreenId}`, {method: 'DELETE'});
            screens = screens.filter(s => s.id !== currentScreenId);
            currentScreenId = null;
            document.getElementById('editor-panel').style.display = 'none';
            document.getElementById('empty-state').style.display = 'block';
            renderScreenList();
        }

        // --- Live Preview ---
        async function runPreview() {
            const l1 = document.getElementById('inp-l1').value;
            const l2 = document.getElementById('inp-l2').value;
            const l3 = document.getElementById('inp-l3').value;
            const out = document.getElementById('preview-output');
            
            out.textContent = 'Загрузка...';
            out.className = 'preview-box';
            
            try {
                const res = await fetch('/api/preview', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({l1, l2, l3})
                });
                const data = await res.json();
                
                if (data.unknown_vars && data.unknown_vars.length > 0) {
                    out.className = 'preview-box preview-error';
                    out.textContent = `Ошибка: неизвестные переменные: ${data.unknown_vars.join(', ')}`;
                    return;
                }
                
                let text = '';
                if (data.rendered_l1) text += `L1: ${data.rendered_l1}\n`;
                if (data.rendered_l2) text += `L2: ${data.rendered_l2}\n`;
                if (data.rendered_l3) text += `L3: ${data.rendered_l3}\n`;
                
                if (!data.all_resolved) {
                    out.className = 'preview-box preview-error';
                    text += '\n[ВНИМАНИЕ: Некоторые переменные не резолвятся. Экран может быть скрыт.]';
                } else {
                    out.className = 'preview-box';
                }
                out.textContent = text || '(пусто)';
                
            } catch (e) {
                out.className = 'preview-box preview-error';
                out.textContent = 'Ошибка сети: ' + e.message;
            }
        }

        // --- Утилиты ---
        function insertVar(text) {
            if (!activeTextarea) {
                alert('Сначала кликните в поле L1, L2 или L3, куда нужно вставить переменную.');
                return;
            }
            const start = activeTextarea.selectionStart;
            const end = activeTextarea.selectionEnd;
            const val = activeTextarea.value;
            activeTextarea.value = val.substring(0, start) + text + val.substring(end);
            activeTextarea.focus();
            activeTextarea.selectionStart = activeTextarea.selectionEnd = start + text.length;
        }

        function escapeHtml(str) {
            if (!str) return '';
            return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        }

        // Запуск
        document.addEventListener('DOMContentLoaded', init);
    </script>
</body>
</html>
"""

MANIFEST_JSON = {
    "name": "shkaf-hud",
    "short_name": "shkaf-hud",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#17181a",
    "theme_color": "#ff8c2f",
    "icons": [
        {
            "src": "https://raw.githubusercontent.com/RGCustom/shkaf-hud/main/icon.png",
            "sizes": "512x512",
            "type": "image/png",
        }
    ],
}

SW_JS = """
// shkaf-hud service worker - минимальный, только для установки как PWA.
// Данные всегда живые (network-first), офлайн-кэш тут не имеет особого смысла.
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => self.clients.claim());
self.addEventListener('fetch', e => {
    e.respondWith(fetch(e.request).catch(() => new Response('offline', {status: 503})));
});
"""


def register_screens_routes(app, get_context):
    @app.route("/screens")
    def screens_page():
        return Response(SCREENS_PAGE_HTML, mimetype="text/html")

    @app.route("/manifest.json")
    def manifest():
        return Response(json.dumps(MANIFEST_JSON), mimetype="application/manifest+json")

    @app.route("/sw.js")
    def service_worker():
        return Response(SW_JS, mimetype="application/javascript")

    @app.route("/api/variables")
    def api_variables():
        return jsonify(variables.legend())

    @app.route("/api/preview", methods=["POST"])
    def api_preview():
        body = request.get_json(force=True)
        ctx = get_context()
        
        results = {}
        all_resolved = True
        unknown_vars = set()
        
        for key in ("l1", "l2", "l3"):
            tpl = body.get(key, "")
            if not tpl:
                results[f"rendered_{key}"] = ""
                continue
                
            unk = templates.validate_template(tpl)
            if unk:
                unknown_vars.update(unk)
                
            rendered, ok = templates.render(tpl, ctx, index=0)
            results[f"rendered_{key}"] = rendered
            if not ok:
                all_resolved = False
                
        if unknown_vars:
            return jsonify({"rendered_l1": "", "rendered_l2": "", "rendered_l3": "", "all_resolved": False, "unknown_vars": list(unknown_vars)})
            
        return jsonify({**results, "all_resolved": all_resolved, "unknown_vars": []})

    @app.route("/api/screens", methods=["GET"])
    def api_screens_list():
        with _lock:
            return jsonify(_screens)

    @app.route("/api/screens", methods=["POST"])
    def api_screens_create():
        body = request.get_json(force=True)
        with _lock:
            new_list, screen = screens.create_screen(_screens, body)
            _screens[:] = new_list
            screens.save_screens(_screens)
            return jsonify(screen)

    @app.route("/api/screens/<screen_id>", methods=["PUT"])
    def api_screens_update(screen_id):
        body = request.get_json(force=True)
        with _lock:
            new_list, screen = screens.update_screen(_screens, screen_id, body)
            _screens[:] = new_list
            screens.save_screens(_screens)
            if screen is None:
                return jsonify({"error": "not found"}), 404
            return jsonify(screen)

    @app.route("/api/screens/<screen_id>", methods=["DELETE"])
    def api_screens_delete(screen_id):
        with _lock:
            _screens[:] = screens.delete_screen(_screens, screen_id)
            screens.save_screens(_screens)
            return jsonify({"ok": True})

    @app.route("/api/screens/reorder", methods=["POST"])
    def api_screens_reorder():
        body = request.get_json(force=True)
        order = body.get("order", [])
        with _lock:
            _screens[:] = screens.reorder_screens(_screens, order)
            screens.save_screens(_screens)
            return jsonify({"ok": True})


def get_screens():
    """Для главного цикла - актуальный список экранов (с учётом правок из веб-интерфейса)."""
    with _lock:
        return list(_screens)