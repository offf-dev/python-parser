// Главная страница: AJAX-парсинг через Parse-кнопку.

document.addEventListener('DOMContentLoaded', () => {
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

        try {
            const res = await fetch('/parse_now', { method: 'POST', body: new FormData(form) });
            const data = await res.json();
            if (data.success) {
                resultDiv.innerHTML = `
                    <div class="alert alert-success">Успешно спаршено ${data.count} статей с ${data.resource_name || 'ресурса'}!</div>
                    <h3>Результат парсинга (${data.count} статей)</h3>
                    ${data.table}
                `;
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
        }
    });
});

function clearAll() {
    document.getElementById('parseForm').reset();
    const r = document.getElementById('parseResult');
    if (r) r.innerHTML = '';
    document.querySelectorAll('.alert, .table-result').forEach(el => el.remove());
}

// ====================== Emoji picker ======================
const EMOJIS = [
    '🎨','🖌️','✨','💎','🔨','🛠️','⚙️','🔧',
    '💻','⚛️','🧩','🪲','📦','📡','⚡','🚀',
    '📝','📰','📚','📖','✏️','📌','🔖','🎓',
    '🌐','🔗','🖥️','📱','🐴','🔥','🎯','💡',
    '🎬','🎤','📊','🧪','🔍','🧠','🪄','🌶️',
];

const saveBtn = document.getElementById('saveBtn');
if (saveBtn) {
    saveBtn.addEventListener('click', (e) => {
        // Перехватываем submit и сначала открываем picker
        e.preventDefault();
        openEmojiModal();
    });
}

function openEmojiModal() {
    const grid = document.getElementById('emojiGrid');
    const current = document.getElementById('emojiInput').value;
    grid.innerHTML = EMOJIS.map(em => {
        const sel = em === current ? ' emoji-btn--selected' : '';
        return `<button type="button" class="emoji-btn${sel}" onclick="setEmojiAndSubmit('${em}')">${em}</button>`;
    }).join('');
    document.getElementById('emojiModal').style.display = 'flex';
}

function closeEmojiModal() {
    document.getElementById('emojiModal').style.display = 'none';
}

function setEmojiAndSubmit(emoji) {
    document.getElementById('emojiInput').value = emoji;
    closeEmojiModal();
    // Submit с правильным action=save
    const form = document.getElementById('parseForm');
    const actionInput = document.createElement('input');
    actionInput.type = 'hidden';
    actionInput.name = 'action';
    actionInput.value = 'save';
    form.appendChild(actionInput);
    form.submit();
}

// Esc закрывает модалку
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeEmojiModal();
});