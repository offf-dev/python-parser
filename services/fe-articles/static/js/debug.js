// /debug: получаем HTML страницы, затем пошагово подбираем селекторы.
//
// HTML живёт на сервере под токеном — шаги с селекторами не дёргают Playwright
// заново, поэтому проверка идёт мгновенно и её не жалко запускать на каждый
// ввод (с дебаунсом).

let DEBUG_TOKEN = null;
let RAW_HTML = '';

const DEBOUNCE_MS = 400;

document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('debugForm');
    if (!form) return;
    const btn = document.getElementById('getHtmlBtn');
    const originalText = btn.textContent;

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        btn.disabled = true;
        btn.textContent = 'Загрузка...';
        document.getElementById('loading').style.display = 'block';
        document.getElementById('debugResult').innerHTML = '';
        resetWizard();

        const fd = new FormData(form);
        fd.append('ajax', 'true');
        try {
            const res = await fetch('/debug', { method: 'POST', body: fd });
            const data = await res.json();
            if (!data.ok) {
                document.getElementById('debugResult').innerHTML =
                    `<div class="alert alert-error">${data.error || 'Не удалось получить HTML'}</div>`;
                return;
            }
            DEBUG_TOKEN = data.token;
            RAW_HTML = data.html;
            renderHtmlBlock(data);
            document.getElementById('selectorWizard').style.display = 'block';
            document.getElementById('sel-item').focus();
            runCheck();
        } catch (err) {
            document.getElementById('debugResult').innerHTML =
                `<div class="alert alert-error">Ошибка: ${err.message}</div>`;
        } finally {
            btn.disabled = false;
            btn.textContent = originalText;
            document.getElementById('loading').style.display = 'none';
            if (typeof hideLoader === 'function') hideLoader();
        }
    });

    ['sel-item', 'sel-title', 'sel-link'].forEach(id => {
        const input = document.getElementById(id);
        if (!input) return;
        let timer = null;
        input.addEventListener('input', () => {
            clearTimeout(timer);
            timer = setTimeout(runCheck, DEBOUNCE_MS);
        });
    });
});

function resetWizard() {
    DEBUG_TOKEN = null;
    ['item', 'title', 'link'].forEach(k => {
        const i = document.getElementById('sel-' + k);
        if (i) i.value = '';
        const o = document.getElementById('out-' + k);
        if (o) o.innerHTML = '';
    });
    ['step-title', 'step-link', 'step-final'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = 'none';
    });
    const f = document.getElementById('out-final');
    if (f) f.innerHTML = '';
    const w = document.getElementById('selectorWizard');
    if (w) w.style.display = 'none';
}

function renderHtmlBlock(data) {
    document.getElementById('debugResult').innerHTML = `
<div class="toolbar">
    <button class="copy-btn" onclick="copyHTML()">📋 Скопировать</button>
    <button class="copy-btn" onclick="toggleHTML()" id="toggleHtmlBtn">👁 Показать HTML</button>
    <span>Длина: ${data.length} символов</span>
</div>
<pre id="htmlCode" style="display:none;">${data.html}</pre>`;
}

function toggleHTML() {
    const pre = document.getElementById('htmlCode');
    const btn = document.getElementById('toggleHtmlBtn');
    const hidden = pre.style.display === 'none';
    pre.style.display = hidden ? 'block' : 'none';
    btn.textContent = hidden ? '🙈 Скрыть HTML' : '👁 Показать HTML';
}

async function runCheck() {
    if (!DEBUG_TOKEN) return;
    const payload = {
        token: DEBUG_TOKEN,
        item: document.getElementById('sel-item').value.trim(),
        title: document.getElementById('sel-title').value.trim(),
        link: document.getElementById('sel-link').value.trim(),
    };
    let data;
    try {
        const res = await fetch('/debug/check', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        data = await res.json();
    } catch (err) {
        document.getElementById('out-item').innerHTML = alertBox('error', `Ошибка: ${err.message}`);
        return;
    }

    if (data.expired) {
        document.getElementById('out-item').innerHTML = alertBox('error', data.error);
        return;
    }

    renderBlocks(data.blocks);
    renderTitle(data.title);
    renderLink(data.link);
    renderFinal(data.final, data.config);
}

// ====================== Рендер шагов ======================
function alertBox(kind, text) {
    return `<div class="alert alert-${kind}">${text}</div>`;
}

function notesHtml(notes) {
    if (!notes || !notes.length) return '';
    return '<ul class="dbg-notes">' + notes.map(n => `<li>${n}</li>`).join('') + '</ul>';
}

function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function samplesHtml(title, items) {
    if (!items || !items.length) return '';
    return `<div class="dbg-samples"><b>${title}</b><ol>` +
        items.map(s => `<li>${escapeHtml(s)}</li>`).join('') + '</ol></div>';
}

function show(id, on) {
    const el = document.getElementById(id);
    if (el) el.style.display = on ? 'block' : 'none';
}

function renderBlocks(b) {
    const out = document.getElementById('out-item');
    if (!b) { out.innerHTML = ''; show('step-title', false); show('step-link', false); show('step-final', false); return; }
    if (b.error) { out.innerHTML = alertBox('error', b.error); show('step-title', false); return; }

    const kind = b.ok ? (b.uniform ? 'success' : 'info') : 'error';
    const head = b.ok
        ? `Найдено блоков: <b>${b.count}</b> — ${b.uniform ? 'структура одинаковая' : 'структура разная'}`
        : 'Ничего не найдено';

    const groups = (b.groups || []).map(g =>
        `<li><code>${escapeHtml(g.signature)}</code> — ${g.count} шт. (${g.share}%)</li>`).join('');

    out.innerHTML = alertBox(kind, head) + notesHtml(b.notes) +
        (groups ? `<div class="dbg-samples"><b>Структуры найденного:</b><ul>${groups}</ul></div>` : '') +
        (b.sample ? `<details class="dbg-details"><summary>Первый блок целиком</summary><pre>${escapeHtml(b.sample)}</pre></details>` : '');

    show('step-title', !!b.ok);
    if (!b.ok) { show('step-link', false); show('step-final', false); }
}

function renderTitle(t) {
    const out = document.getElementById('out-title');
    if (!t) { out.innerHTML = ''; show('step-link', false); show('step-final', false); return; }
    if (t.error) { out.innerHTML = alertBox('error', t.error); show('step-link', false); return; }

    const kind = !t.ok ? 'error' : (t.found === t.total && !t.empty && !t.with_url ? 'success' : 'info');
    const head = t.ok
        ? `Заголовок найден в <b>${t.found}</b> из ${t.total} блоков, уникальных текстов: ${t.unique}`
        : 'Заголовок не найден ни в одном блоке';

    out.innerHTML = alertBox(kind, head) + notesHtml(t.notes) +
        samplesHtml('Первые заголовки:', t.samples);

    show('step-link', !!t.ok);
    if (!t.ok) show('step-final', false);
}

function renderLink(l) {
    const out = document.getElementById('out-link');
    if (!l) { out.innerHTML = ''; show('step-final', false); return; }
    if (l.error) { out.innerHTML = alertBox('error', l.error); show('step-final', false); return; }

    const kind = !l.ok ? 'error' : (l.found === l.total && !l.anchors ? 'success' : 'info');
    const head = l.ok
        ? `Ссылка найдена в <b>${l.found}</b> из ${l.total} блоков`
        : 'Ссылка не найдена ни в одном блоке';

    out.innerHTML = alertBox(kind, head) + notesHtml(l.notes) +
        samplesHtml('Первые ссылки:', l.samples);

    show('step-final', !!l.ok);
}

function renderFinal(f, cfg) {
    const out = document.getElementById('out-final');
    if (!f) { out.innerHTML = ''; show('step-final', false); return; }
    if (f.error) { out.innerHTML = alertBox('error', f.error); return; }

    const rows = (f.items || []).map((a, i) =>
        `<tr><td>${i + 1}</td><td>${escapeHtml(a.title)}</td><td><a href="${a.url}" target="_blank" rel="noopener">${escapeHtml(a.url)}</a></td></tr>`
    ).join('');

    const head = f.count
        ? `После фильтров останется статей: <b>${f.count}</b> (лимит за проход — ${f.limit})`
        : 'После фильтров не осталось ни одной статьи — блоки есть, но всё отсеялось';

    const cfgBlock = cfg ? `
<div class="dbg-samples"><b>Готовые значения для /sites:</b>
<pre id="dbgConfig">site_url:           ${escapeHtml(cfg.site_url)}
articles_selector:  ${escapeHtml(cfg.articles_selector)}
title_selector:     ${escapeHtml(cfg.title_selector)}
url_selector:       ${escapeHtml(cfg.url_selector)}</pre>
<button class="copy-btn" onclick="copyConfig()">📋 Скопировать конфиг</button></div>` : '';

    out.innerHTML = alertBox(f.count ? 'success' : 'error', head) +
        (rows ? `<table class="dataframe"><thead><tr><th>#</th><th>Заголовок</th><th>URL</th></tr></thead><tbody>${rows}</tbody></table>` : '') +
        cfgBlock;
}

// ====================== Копирование ======================
function copyHTML() {
    const code = document.getElementById('htmlCode');
    if (!code) return;
    const raw = code.innerHTML
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&amp;/g, '&');
    navigator.clipboard.writeText(raw)
        .then(() => alert('Сырой HTML скопирован!'))
        .catch(err => alert('Ошибка: ' + err));
}

function copyConfig() {
    const pre = document.getElementById('dbgConfig');
    if (!pre) return;
    navigator.clipboard.writeText(pre.textContent)
        .then(() => alert('Конфиг скопирован!'))
        .catch(err => alert('Ошибка: ' + err));
}
