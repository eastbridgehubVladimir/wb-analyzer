// TODO: до подключения реальных платежей — добавить серверную валидацию
// tg.initData через HMAC-SHA256 с TELEGRAM_BOT_TOKEN, чтобы исключить
// подделку telegram_user_id. См. документацию:
// https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

// ── Тема ──────────────────────────────────────────────────────────────────────

function isDark(hex) {
  const h = (hex || '#ffffff').replace('#', '');
  const r = parseInt(h.slice(0,2), 16);
  const g = parseInt(h.slice(2,4), 16);
  const b = parseInt(h.slice(4,6), 16);
  return (r * 0.299 + g * 0.587 + b * 0.114) < 128;
}

function applyTheme() {
  const p = tg.themeParams;
  const r = document.documentElement.style;
  r.setProperty('--tg-bg-color',            p.bg_color            || '#ffffff');
  r.setProperty('--tg-text-color',          p.text_color          || '#000000');
  r.setProperty('--tg-hint-color',          p.hint_color          || '#999999');
  r.setProperty('--tg-button-color',        p.button_color        || '#2481cc');
  r.setProperty('--tg-button-text-color',   p.button_text_color   || '#ffffff');
  r.setProperty('--tg-secondary-bg-color',  p.secondary_bg_color  || '#f0f0f0');

  // Glass-переменные: разные значения для тёмной и светлой темы
  const dark = isDark(p.bg_color || '#ffffff');
  r.setProperty('--glass-bg',     dark ? 'rgba(255,255,255,0.07)' : 'rgba(0,0,0,0.04)');
  r.setProperty('--glass-border', dark ? 'rgba(255,255,255,0.10)' : 'rgba(0,0,0,0.07)');
  r.setProperty('--glass-shadow', dark ? '0 4px 20px rgba(0,0,0,0.40)' : '0 4px 20px rgba(0,0,0,0.08)');

  // Акцентный цвет кнопки с прозрачностью для градиентов
  const btn = p.button_color || '#2481cc';
  const bR = parseInt(btn.replace('#','').slice(0,2), 16);
  const bG = parseInt(btn.replace('#','').slice(2,4), 16);
  const bB = parseInt(btn.replace('#','').slice(4,6), 16);
  r.setProperty('--accent-a15', `rgba(${bR},${bG},${bB},0.15)`);
  r.setProperty('--accent-a08', `rgba(${bR},${bG},${bB},0.08)`);
}

applyTheme();
tg.onEvent('themeChanged', applyTheme);

// ── Данные пользователя ───────────────────────────────────────────────────────

const user       = tg.initDataUnsafe?.user;
const userId     = user?.id     || null;
const username   = user?.username || null;
const startParam = tg.initDataUnsafe?.start_param || 'miniapp_direct';

// ── Навигация ─────────────────────────────────────────────────────────────────

let currentNiche = null;

function showScreen(id) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}

// ── Утилиты ───────────────────────────────────────────────────────────────────

function fmtMoney(v) {
  if (!v) return '—';
  if (v >= 1e9) return (v / 1e9).toFixed(1) + ' млрд ₽';
  if (v >= 1e6) return Math.round(v / 1e6) + ' млн ₽';
  return Math.round(v / 1e3) + ' тыс ₽';
}

function fmtNum(v) {
  if (!v) return '—';
  return v.toLocaleString('ru-RU');
}

// ── Топ ниш и trust-строка ────────────────────────────────────────────────────

async function loadTopNiches() {
  try {
    const resp = await fetch('/api/miniapp/top-niches');
    if (!resp.ok) return;
    const data = await resp.json();

    // Trust-строка
    const trustLine = document.getElementById('trust-line');
    if (trustLine) {
      const total = data.total_niches ? data.total_niches.toLocaleString('ru-RU') : '7 500+';
      const date  = data.max_updated_at || '';
      trustLine.textContent = `📊 ${total} ниш Wildberries · Данные на ${date}`;
    }

    // Теги топ-ниш
    if (!data.niches || !data.niches.length) return;
    const section = document.getElementById('top-niches-section');
    const list    = document.getElementById('top-niches-list');
    list.innerHTML = '';
    data.niches.forEach(n => {
      const tag = document.createElement('div');
      tag.className = 'niche-tag';
      const name = n.display_name || n.name;
      const shortName = name.length > 20 ? name.slice(0, 20) + '…' : name;
      tag.innerHTML =
        `<span class="niche-tag-name">${shortName}</span>` +
        `<span class="niche-tag-rev">${fmtMoney(n.revenue_annual)}</span>`;
      tag.addEventListener('click', () => showPreview(n));
      list.appendChild(tag);
    });
    section.classList.remove('hidden');
  } catch (_) {
    // Не показываем ошибку пользователю — секция просто не появится
  }
}

loadTopNiches();

// ── Поиск ─────────────────────────────────────────────────────────────────────

const searchInput  = document.getElementById('search-input');
const searchHint   = document.getElementById('search-hint');
const searchStatus = document.getElementById('search-status');
const resultsList  = document.getElementById('results-list');

let searchTimer = null;

searchInput.addEventListener('input', () => {
  clearTimeout(searchTimer);
  const q = searchInput.value.trim();

  if (q.length < 2) {
    searchHint.classList.remove('hidden');
    searchStatus.classList.add('hidden');
    resultsList.innerHTML = '';
    return;
  }

  searchHint.classList.add('hidden');
  searchStatus.textContent = 'Ищу...';
  searchStatus.classList.remove('hidden');
  resultsList.innerHTML = '';

  searchTimer = setTimeout(() => doSearch(q), 500);
});

async function doSearch(q) {
  try {
    const resp = await fetch(`/api/miniapp/search?q=${encodeURIComponent(q)}`);
    if (!resp.ok) throw new Error('Ошибка сервера');
    const niches = await resp.json();

    searchStatus.classList.add('hidden');

    if (!niches.length) {
      searchStatus.textContent = `По запросу «${q}» ничего не найдено`;
      searchStatus.classList.remove('hidden');
      return;
    }

    resultsList.innerHTML = '';
    niches.forEach(n => {
      const el = document.createElement('div');
      el.className = 'result-item';
      el.innerHTML = `
        <div class="result-name">${n.display_name || n.name}</div>
        <div class="result-meta">
          <span>💰 ${fmtMoney(n.revenue_annual)}/год</span>
          <span>🛒 ${fmtNum(n.orders_monthly)}/мес</span>
        </div>
      `;
      el.addEventListener('click', () => showPreview(n));
      resultsList.appendChild(el);
    });
  } catch (e) {
    searchStatus.textContent = 'Ошибка поиска. Попробуйте ещё раз.';
    searchStatus.classList.remove('hidden');
  }
}

// ── Превью ────────────────────────────────────────────────────────────────────

function showPreview(n) {
  currentNiche = n;

  document.getElementById('preview-title').textContent  = n.display_name || n.name;
  document.getElementById('m-revenue').textContent      = fmtMoney(n.revenue_annual);
  document.getElementById('m-orders').textContent       = fmtNum(n.orders_monthly);
  document.getElementById('m-sellers').textContent      = `${fmtNum(n.sellers_active)} / ${fmtNum(n.sellers_total)}`;
  document.getElementById('m-buyout').textContent       = n.buyout_pct ? Math.round(n.buyout_pct * 100) + '%' : '—';
  document.getElementById('m-date').textContent         = n.data_updated_at ? 'Данные на ' + n.data_updated_at : '';

  const pdfStatus = document.getElementById('pdf-status');
  pdfStatus.className = 'pdf-status hidden';
  pdfStatus.innerHTML = '';

  ['btn-basic', 'btn-standard', 'btn-deep'].forEach(id => {
    document.getElementById(id).disabled = false;
  });

  showScreen('screen-preview');
}

document.getElementById('btn-back').addEventListener('click', () => {
  showScreen('screen-search');
});

// ── PDF-кнопки ────────────────────────────────────────────────────────────────

document.getElementById('btn-basic').addEventListener('click', () => orderPdf('basic'));
document.getElementById('btn-standard').addEventListener('click', () => orderPdf('standard'));
document.getElementById('btn-deep').addEventListener('click', () => orderPdf('deep'));

async function orderPdf(level) {
  const pdfStatus = document.getElementById('pdf-status');

  ['btn-basic', 'btn-standard', 'btn-deep'].forEach(id => {
    document.getElementById(id).disabled = true;
  });

  if (level === 'basic') {
    pdfStatus.className = 'pdf-status generating';
    pdfStatus.innerHTML =
      '<div class="generating-block">' +
        '<div class="gen-ring"></div>' +
        '<div class="gen-info">' +
          '<span class="gen-label">Генерирую PDF…</span>' +
          '<span class="gen-sub">обычно около минуты</span>' +
        '</div>' +
      '</div>';
    pdfStatus.classList.remove('hidden');
  }

  try {
    const resp = await fetch('/api/miniapp/order', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        telegram_user_id: userId,
        telegram_username: username,
        niche_name: currentNiche.name,
        pdf_level: level,
        ref_source: startParam,
      }),
    });

    const data = await resp.json();

    if (level === 'basic') {
      if (data.job_id) {
        pollJob(data.job_id);
      } else {
        pdfStatus.className = 'pdf-status error';
        pdfStatus.textContent = 'Ошибка запуска генерации. Попробуйте ещё раз.';
        resetButtons();
      }
    } else {
      pdfStatus.className = 'pdf-status pending';
      pdfStatus.textContent = data.message || 'Оплата временно недоступна.';
      pdfStatus.classList.remove('hidden');
      resetButtons();
    }
  } catch (e) {
    pdfStatus.className = 'pdf-status error';
    pdfStatus.textContent = 'Ошибка соединения. Попробуйте ещё раз.';
    pdfStatus.classList.remove('hidden');
    resetButtons();
  }
}

function resetButtons() {
  ['btn-basic', 'btn-standard', 'btn-deep'].forEach(id => {
    document.getElementById(id).disabled = false;
  });
}

// ── Поллинг генерации PDF ─────────────────────────────────────────────────────

async function pollJob(jobId) {
  const pdfStatus = document.getElementById('pdf-status');
  const BASE_URL  = window.location.origin;

  for (let i = 0; i < 60; i++) {
    await new Promise(r => setTimeout(r, 3000));
    try {
      const resp = await fetch(`/api/miniapp/status/${jobId}`);
      const data = await resp.json();

      if (data.status === 'ready') {
        const absoluteUrl = window.location.origin + data.download_url;
        pdfStatus.className = 'pdf-status ready';
        pdfStatus.innerHTML =
          '✅ PDF готов!<br>' +
          '<button class="download-link" id="btn-download-pdf">⬇️ Скачать Basic PDF</button>';

        document.getElementById('btn-download-pdf').addEventListener('click', () => {
          // tg.downloadFile — лучший вариант (Telegram 10+), скачивает напрямую
          // tg.openLink — открывает в системном браузере где PDF можно сохранить
          // fallback — для десктопного браузера при тестировании вне Telegram
          if (tg.downloadFile) {
            tg.downloadFile({ url: absoluteUrl, file_name: 'WBAnalyzer_Basic.pdf' });
          } else if (tg.openLink) {
            tg.openLink(absoluteUrl);
          } else {
            window.open(absoluteUrl, '_blank');
          }
        });

        resetButtons();
        return;
      }
      if (data.status === 'error') {
        pdfStatus.className = 'pdf-status error';
        pdfStatus.textContent = '❌ Ошибка генерации. Попробуйте ещё раз.';
        resetButtons();
        return;
      }
      // status === 'generating' → continue polling
    } catch (_) {
      // network blip, keep polling
    }
  }

  // timeout after 3 min
  pdfStatus.className = 'pdf-status error';
  pdfStatus.textContent = '⏱ Превышено время ожидания. Попробуйте ещё раз.';
  resetButtons();
}
