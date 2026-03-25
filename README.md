# AutoCars Scraper Demo

A self-contained demo showing a complete scraping setup:
**Vue SPA target site → Python proxy module → Playwright scraper**

Built as a portfolio piece for automotive scraping work.

---

## Project Structure

```
autocars-scraper-demo/
├── frontend/
│   └── index.html          ← Vue 3 SPA (120 vehicle listings, JS pagination)
└── scraper/
    ├── proxy_utils.py       ← ProxyManager — drop-in proxy pool helper
    ├── scraper.py           ← Main scraper (Playwright, dedup, JSON output)
    └── output/              ← Created on first run
        ├── run_YYYYMMDD_HHMMSS.json
        ├── autocars_all.json  (cumulative store)
        └── scraper.log
```

---

## Prerequisites

```bash
# Install project dependencies from pyproject.toml
uv sync

# Install Chromium for Playwright
uv run playwright install chromium
```

If you do not have uv installed yet:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Running the Demo

### 1 — Serve the Vue SPA

```bash
cd frontend
uv run python -m http.server 8080
```

Open **http://localhost:8080** — you should see the AutoCars listing grid.

### 2 — Run the scraper

```bash
cd scraper

# Scrape all pages (120 listings across 10 pages)
uv run scraper.py

# Scrape first 3 pages only (quick demo)
uv run scraper.py --max-pages 3

# Watch it run in a visible browser window
uv run scraper.py --max-pages 3 --visible

# Re-run without dedup (re-collect everything)
uv run scraper.py --no-dedup
```

---

## What the scraper demonstrates

| Requirement | Implementation |
|---|---|
| JS-rendered content | Playwright (Chromium) — waits for Vue to render |
| Pagination | Clicks numbered page buttons, waits for loading overlay |
| Duplicate prevention | Seen-ID set + checks cumulative store on startup |
| Proxy integration | `ProxyManager.get()` — swap in real proxies with no scraper changes |
| Rotating user agents | 5 real UA strings, random per session |
| Retry logic | 3 attempts with exponential back-off |
| Structured output | JSON with all fields + `scraped_at` UTC timestamp |
| Logging | Timestamped to stdout + `output/scraper.log` |

---

## Output format

Each listing record:

```json
{
  "id":           "AC-2024100",
  "make":         "Toyota",
  "model":        "Fortuner",
  "year":         2021,
  "price":        485000,
  "mileage":      62400,
  "transmission": "Automatic",
  "fuel":         "Diesel",
  "color":        "Pearl White",
  "dealer":       "AutoNation Sandton",
  "location":     "Sandton, GP",
  "listed":       "2026-03-14",
  "images": [
    "https://picsum.photos/seed/110/600/400",
    "https://picsum.photos/seed/111/600/400"
  ],
  "scraped_at":   "2026-03-24T09:15:22+00:00"
}
```

---

## Plugging in real proxies

Edit `proxy_utils.py` — replace `_PROXY_POOL`:

```python
_PROXY_POOL = [
    "http://user:pass@gate.brightdata.com:22225",
    "http://user:pass@proxy2.example.com:8080",
    # ...
]
```

The scraper calls `proxy_manager.get()` and `proxy_manager.report_failure()` —
no other changes needed.