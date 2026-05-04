// /links: toggle is_send, delete, debounced search по двум полям.

let debounceTimer;
function debounce(fn, delay) {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(fn, delay);
}

function updatePage(url) {
    showLoader();
    fetch(url)
        .then(r => r.text())
        .then(html => {
            const tmp = document.createElement('div');
            tmp.innerHTML = html;
            document.querySelector('.container').innerHTML = tmp.querySelector('.container').innerHTML;
            history.pushState(null, '', url);
            attachEventListeners();
        })
        .catch(err => console.error('updatePage:', err))
        .finally(() => hideLoader());
}

function handleInput() {
    debounce(() => {
        const t = document.querySelector('#searchTitleInput').value;
        const s = document.querySelector('#searchSiteInput').value;
        const perPage = new URLSearchParams(location.search).get('per_page') || DEFAULT_PER_PAGE;
        let url = `/links?page=1&per_page=${perPage}`;
        if (t) url += `&search_title=${encodeURIComponent(t)}`;
        if (s) url += `&search_site=${encodeURIComponent(s)}`;
        updatePage(url);
    }, 300);
}

function attachEventListeners() {
    document.querySelectorAll('.pagination a').forEach(a => {
        a.addEventListener('click', e => {
            e.preventDefault();
            updatePage(a.href);
        });
    });
    const t = document.querySelector('#searchTitleInput');
    const s = document.querySelector('#searchSiteInput');
    if (t) t.addEventListener('input', handleInput);
    if (s) s.addEventListener('input', handleInput);
}

function toggleLink(url, isSend) {
    showLoader();
    fetch(`/links/toggle?url=${encodeURIComponent(url)}&is_send=${isSend}`)
        .then(r => { if (!r.ok) throw new Error(r.status); location.reload(); })
        .catch(err => { hideLoader(); alert('Ошибка: ' + err.message); });
}

function deleteLink(siteName, url) {
    if (!confirm(`Удалить статью с URL "${url}"?`)) return;
    showLoader();
    fetch('/links/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ site_name: siteName, url }),
    }).then(() => location.reload()).catch(() => hideLoader());
}

function resetFilter() {
    const perPage = new URLSearchParams(location.search).get('per_page') || DEFAULT_PER_PAGE;
    updatePage(`/links?page=1&per_page=${perPage}`);
}

document.addEventListener('DOMContentLoaded', attachEventListeners);