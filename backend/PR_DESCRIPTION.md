PDF Export Feature + CI Setup
=============================

### Что сделано

- Добавлен endpoint `/api/v1/analysis/category/export-pdf` с поддержкой трёх уровней отчёта: `basic`, `standard`, `deep`.
- Добавлен режим `stub=true`, позволяющий генерировать PDF локально без внешних запросов.
- Сгенерирован PDF содержит:
  - verdict/score
  - ключевые метрики
  - server-side графики (price distribution, top revenue)
  - agent outputs: decision_engine dimensions + AI insights/hypotheses/analysis
- Реализован MPStats-first путь: при наличии `mpstats_token` данные берутся из MPStats, иначе используется существующий scraper fallback.
- Добавлен frontend пример с тремя кнопками и curl-примеры для API.
- Добавлен smoke тест endpoint в `backend/tests/test_export_endpoint.py`.
- Настроен GitHub Actions workflow: `.github/workflows/ci.yml`.
- Обновлены зависимости `backend/requirements.txt` для CI: `requests`, `pytest`.

### Как протестировать

#### Локально

```bash
cd backend
source ../venv311/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q tests/test_export_endpoint.py
```

#### Через curl

```bash
curl -X POST 'http://127.0.0.1:8001/api/v1/analysis/category/export-pdf?stub=true' \
  -H 'Content-Type: application/json' \
  -d '{"category":"Demo","max_products":3,"report_level":"standard"}' \
  --output wb_report_standard.pdf
```

#### Пример JS

```html
<button id="btn-basic">Export Basic</button>
<button id="btn-standard">Export Standard</button>
<button id="btn-deep">Export Deep</button>

<script>
async function downloadReport(level) {
  const resp = await fetch('/api/v1/analysis/category/export-pdf?stub=true', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ category: 'Demo', max_products: 10, report_level: level }),
  });
  if (!resp.ok) { alert('Export failed: ' + resp.status); return; }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `wb_report_${level}.pdf`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

document.getElementById('btn-basic').onclick = () => downloadReport('basic');
document.getElementById('btn-standard').onclick = () => downloadReport('standard');
document.getElementById('btn-deep').onclick = () => downloadReport('deep');
</script>
```

### Примечания

- Для реального MPStats-пути нужен `mpstats_token` в `.env`.
- Для AI-инсайтов нужен `anthropic_api_key`, иначе используется демо-аналитика.
