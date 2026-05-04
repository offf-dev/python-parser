// /domains: клик по эмодзи открывает picker, выбор → ajax POST → обновляем ячейку.

const EMOJIS = [
    '🎨','🖌️','🖼️','✨','💎','🌈','🎭','🪞',
    '🔨','🛠️','⚙️','🔧','📐','🧰','🪛','🔩','⚖️','🪚','🪜',
    '💻','⚛️','🧩','🪲','📦','📡','⚡','🤖','🔌','🖱️','⌨️','🖨️','🛜',
    '📝','📰','📚','📖','✏️','📌','🔖','📑','📓','📔','📜',
    '🌐','🔗','🖥️','📱','🪟','📺','☁️','📲',
    '🚀','🔥','🎯','💡','🎬','🎤','📊','🧪','🔍','🧠','🪄',
    '⭐','💫','✅','🎁','🏆','🛡️','🪐','🛰️',
    '🐴','🦄','🐵','🦊','🐢','🐙','🐝','🦉',
    '🌶️','🍕','☕','🍿',
];

let currentDomainId = null;

function openEmojiPicker(domainId) {
    currentDomainId = domainId;
    const row = document.querySelector(`tr[data-id="${domainId}"]`);
    const currentEmoji = row ? row.querySelector('.emoji-cell').textContent.trim() : '';

    const grid = document.getElementById('emojiGrid');
    grid.innerHTML = EMOJIS.map(em => {
        const sel = em === currentEmoji ? ' emoji-btn--selected' : '';
        return `<button type="button" class="emoji-btn${sel}" onclick="setEmojiForDomain('${em}')">${em}</button>`;
    }).join('');

    const currentEl = document.getElementById('emojiCurrent');
    const currentVal = document.getElementById('emojiCurrentValue');
    if (currentEmoji) {
        currentVal.textContent = currentEmoji;
        currentEl.style.display = 'block';
    } else {
        currentEl.style.display = 'none';
    }

    const customInput = document.getElementById('emojiCustomInput');
    customInput.value = currentEmoji && !EMOJIS.includes(currentEmoji) ? currentEmoji : '';

    document.getElementById('emojiModal').style.display = 'flex';
}

function closeEmojiModal() {
    document.getElementById('emojiModal').style.display = 'none';
    currentDomainId = null;
}

function applyCustomEmoji() {
    const v = document.getElementById('emojiCustomInput').value.trim();
    if (v) setEmojiForDomain(v);
}

async function setEmojiForDomain(emoji) {
    if (!currentDomainId || !emoji) return;
    const id = currentDomainId;
    try {
        const r = await fetch(`/domains/${id}/emoji`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ emoji }),
        });
        const data = await r.json();
        if (r.ok && data.success) {
            // Обновляем ячейку
            const row = document.querySelector(`tr[data-id="${id}"]`);
            if (row) row.querySelector('.emoji-cell').textContent = data.emoji;
            closeEmojiModal();
        } else {
            alert('Ошибка: ' + (data.error || r.status));
        }
    } catch (e) {
        alert('Сетевая ошибка: ' + e.message);
    }
}

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeEmojiModal();
});