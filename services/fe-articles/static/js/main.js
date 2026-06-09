// Главная страница: AJAX-парсинг через Parse-кнопку.

let lastParsed = null;  // { resource_name, articles, readonly, db_articles }

document.addEventListener('DOMContentLoaded', () => {
    // Гидрация server-rendered плашки: если шаблон уже отрисовал кнопку
    // (приход через GET /?parse_now=ID с /sites), вытаскиваем articles из JSON-блока.
    const seed = document.getElementById('serverParsed');
    if (seed) {
        try {
            const parsed = JSON.parse(seed.textContent);
            lastParsed = {
                resource_name: parsed.resource_name,
                articles: parsed.articles || [],
                readonly: !!parsed.readonly,
                db_articles: true,
            };
            wireSaveButton();
        } catch (e) { /* ignore */ }
    }

    const btn = document.getElementById('parseBtn');
    if (!btn) return;

    btn.addEventListener('click', async (e) => {
        e.preventDefault();
        const form = document.getElementById('parseForm');
        const loading = document.getElementById('loading');
        const resultDiv = document.getElementById('parseResult');

        const originalText = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'Парсинг...';
        loading.style.display = 'block';
        resultDiv.innerHTML = '';
        lastParsed = null;

        try {
            const res = await fetch('/parse_now', { method: 'POST', body: new FormData(form) });
            const data = await res.json();
            if (data.success) {
                lastParsed = {
                    resource_name: data.resource_name,
                    articles: data.articles || [],
                    readonly: !!data.readonly,
                    db_articles: !!data.db_articles,
                };
                resultDiv.innerHTML = `
                    <div class="alert alert-success alert-with-action" id="parseSuccess">
                        <span>Успешно спаршено ${data.count} статей с ${data.resource_name || 'ресурса'}!</span>
                        ${renderSaveButton()}
                    </div>
                    <h3>Результат парсинга (${data.count} статей)</h3>
                    ${data.table}
                `;
                wireSaveButton();
                resultDiv.scrollIntoView({ behavior: 'smooth', block: 'start' });
            } else {
                resultDiv.innerHTML = `<div class="alert alert-error">${data.error || 'Неизвестная ошибка'}</div>`;
            }
        } catch (err) {
            resultDiv.innerHTML = `<div class="alert alert-error">Ошибка соединения: ${err.message}</div>`;
        } finally {
            btn.disabled = false;
            btn.textContent = originalText;
            loading.style.display = 'none';
            hideLoader();
        }
    });
});

function renderSaveButton() {
    if (!lastParsed || !lastParsed.articles.length) return '';
    if (lastParsed.readonly) {
        return `<button type="button" class="btn-save-prod" disabled title="БД в READONLY — подними write-compose">Сохранить в БД (PROD)</button>`;
    }
    return `<button type="button" class="btn-save-prod" id="saveProdBtn">Сохранить в БД (PROD)</button>`;
}

function wireSaveButton() {
    const sbtn = document.getElementById('saveProdBtn');
    if (!sbtn) return;
    sbtn.addEventListener('click', async () => {
        if (!lastParsed) return;
        const orig = sbtn.textContent;
        sbtn.disabled = true;
        sbtn.textContent = 'Сохраняю...';
        try {
            const res = await fetch('/save_parsed', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    resource_name: lastParsed.resource_name,
                    articles: lastParsed.articles,
                }),
            });
            const data = await res.json();
            const banner = document.getElementById('parseSuccess');
            if (data.success) {
                const msg = data.message
                    ? data.message
                    : `Сохранено ${data.saved} новых · дубликатов пропущено ${data.skipped_dup} (из ${data.submitted})`;
                banner.classList.remove('alert-error');
                banner.classList.add('alert-success');
                banner.innerHTML = `<span>✓ ${msg}</span>`;
            } else {
                banner.classList.remove('alert-success');
                banner.classList.add('alert-error');
                banner.innerHTML = `<span>Ошибка: ${data.error || 'неизвестная'}</span>${renderSaveButton()}`;
                wireSaveButton();
                sbtn.disabled = false;
                sbtn.textContent = orig;
            }
        } catch (err) {
            sbtn.disabled = false;
            sbtn.textContent = orig;
            alert('Ошибка соединения: ' + err.message);
        }
    });
}

function clearAll() {
    document.getElementById('parseForm').reset();
    const r = document.getElementById('parseResult');
    if (r) r.innerHTML = '';
    document.querySelectorAll('.alert, .table-result').forEach(el => el.remove());
    lastParsed = null;
}