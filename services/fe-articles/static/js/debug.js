// /debug: получить HTML страницы + копирование в буфер.

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

        const fd = new FormData(form);
        fd.append('ajax', 'true');
        try {
            const res = await fetch('/debug', { method: 'POST', body: fd });
            const html = await res.text();
            document.getElementById('debugResult').innerHTML = html;
        } catch (err) {
            document.getElementById('debugResult').innerHTML = `<div class="alert alert-error">Ошибка: ${err.message}</div>`;
        } finally {
            btn.disabled = false;
            btn.textContent = originalText;
            document.getElementById('loading').style.display = 'none';
        }
    });
});

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