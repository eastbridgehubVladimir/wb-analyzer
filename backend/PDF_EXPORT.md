PDF export
==========

This document explains how to use the PDF export endpoint for WB‑Analyzer.

Endpoint
--------
POST /api/v1/analysis/category/export-pdf

Query parameters:
- `stub=true` — generate PDF from demo data (no external requests)

Request body (JSON):
- `category` (string)
- `max_products` (int)
- `scrape_pages` (int)
- `report_level` (string) — `basic` | `standard` | `deep`

Examples
--------
Generate `standard` report using demo stub:

```bash
curl -X POST 'http://127.0.0.1:8001/api/v1/analysis/category/export-pdf?stub=true' \
  -H 'Content-Type: application/json' \
  -d '{"category":"Demo","max_products":3,"report_level":"standard"}' \
  --output wb_report_standard.pdf
```

Generate `deep` report (MPStats if configured, else scraper fallback):

```bash
curl -X POST 'http://127.0.0.1:8001/api/v1/analysis/category/export-pdf' \
  -H 'Content-Type: application/json' \
  -d '{"category":"смартфоны","max_products":10,"report_level":"deep"}' \
  --output wb_report_deep.pdf
```

Front-end example (three buttons):

```html
<div>
  <button id="btn-basic">Export Basic</button>
  <button id="btn-standard">Export Standard</button>
  <button id="btn-deep">Export Deep</button>
</div>
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

Notes
-----
- If `MPSTATS` token is configured in `.env` (`mpstats_token`), the endpoint will prefer MPStats data. Otherwise it falls back to the scraper (Playwright).
- Use `stub=true` for offline development.
- The returned filename contains the category and report level.
