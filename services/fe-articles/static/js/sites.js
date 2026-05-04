// /sites: toggle active, delete, debounced search.

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

function attachEventListeners() {
    document.querySelectorAll('.pagination a').forEach(a => {
        a.addEventListener('click', e => {
            e.preventDefault();
            updatePage(a.href);
        });
    });

    const search = document.querySelector('#searchInput');
    if (search) {
        search.addEventListener('input', () => {
            debounce(() => {
                const q = search.value;
                const perPage = new URLSearchParams(location.search).get('per_page') || DEFAULT_PER_PAGE;
                updatePage(`/sites?search=${encodeURIComponent(q)}&page=1&per_page=${perPage}`);
            }, 300);
        });
    }
}

function toggleCheckbox(identifier, isActive) {
    if (!identifier || identifier === 'None') {
        console.error('toggleCheckbox: пустой identifier');
        return;
    }
    showLoader();
    fetch(`/sites/toggle?identifier=${encodeURIComponent(identifier)}&active=${isActive}`)
        .then(r => {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            location.reload();
        })
        .catch(err => { hideLoader(); console.error('toggle:', err); });
}

function deleteSite(siteId, siteKey) {
    if (!confirm(`Удалить сайт "${siteKey}"?`)) return;
    showLoader();
    fetch('/sites/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: siteId }),
    }).then(() => location.reload()).catch(() => hideLoader());
}

function resetFilter() {
    const perPage = new URLSearchParams(location.search).get('per_page') || DEFAULT_PER_PAGE;
    updatePage(`/sites?page=1&per_page=${perPage}`);
}

document.addEventListener('DOMContentLoaded', attachEventListeners);