"""
scraper.py
----------
AutoCars demo scraper — targets the Vue SPA at localhost:8080.

Features
--------
  ✓  Playwright (Chromium) — handles JS-rendered, SPA-paginated content
  ✓  Plugs into proxy_utils.ProxyManager (client's existing helper)
  ✓  Rotating User-Agent headers
  ✓  Configurable retry logic with exponential back-off
  ✓  Duplicate prevention via seen-ID set + output file check
  ✓  Structured JSON output (one file per run + cumulative store)
  ✓  Timestamped logging to console and scraper.log
  ✓  Graceful shutdown on Ctrl-C
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Optional Playwright import — friendly error if not installed
# ---------------------------------------------------------------------------
try:
    from playwright.sync_api import (
        sync_playwright,
        Page,
        TimeoutError as PWTimeout,
        Error as PWError,
    )
except ImportError:
    print(
        "\n[ERROR] Playwright not installed.\n"
        "Run:  uv add playwright && uv run playwright install chromium\n"
    )
    sys.exit(1)

from proxy_utils import ProxyManager

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL       = "http://localhost:8080"
OUTPUT_DIR     = Path("output")
STORE_FILE     = OUTPUT_DIR / "autocars_all.json"
PER_PAGE       = 12           # must match the Vue app
MAX_RETRIES    = 3
RETRY_DELAY    = 2.0          # seconds (doubles on each retry)
PAGE_TIMEOUT   = 15_000       # ms — Playwright timeout
HEADLESS       = True         # set False to watch the browser

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
OUTPUT_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(OUTPUT_DIR / "scraper.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_seen_ids() -> set[str]:
    """Load previously scraped listing IDs to prevent duplicates."""
    if STORE_FILE.exists():
        try:
            with open(STORE_FILE, encoding="utf-8") as f:
                existing = json.load(f)
            ids = {item["id"] for item in existing}
            log.info("Loaded %d existing IDs from store.", len(ids))
            return ids
        except (json.JSONDecodeError, KeyError):
            log.warning("Store file unreadable — starting fresh.")
    return set()


def save_to_store(listings: list[dict]):
    """Append new listings to the cumulative store, deduplicating by ID."""
    existing = []
    if STORE_FILE.exists():
        try:
            with open(STORE_FILE, encoding="utf-8") as f:
                existing = json.load(f)
        except json.JSONDecodeError:
            pass

    existing_ids = {item["id"] for item in existing}
    new_items    = [l for l in listings if l["id"] not in existing_ids]
    merged       = existing + new_items

    with open(STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    log.info("Store updated — %d total records (%d new).", len(merged), len(new_items))
    return new_items


def save_run_file(listings: list[dict]) -> Path:
    """Write this run's results to a timestamped file."""
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_file = OUTPUT_DIR / f"run_{ts}.json"
    with open(run_file, "w", encoding="utf-8") as f:
        json.dump(listings, f, indent=2, ensure_ascii=False)
    log.info("Run file saved: %s", run_file)
    return run_file


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_cards(page: Page) -> list[dict]:
    """
    Extract all listing cards from the current DOM.
    Cards expose their data via data-* attributes set by Vue — no fragile
    CSS-class selectors needed.
    """
    cards = page.query_selector_all("[data-listing-id]")
    results = []

    for card in cards:
        def attr(name: str) -> str:
            return card.get_attribute(f"data-{name}") or ""

        # Image URLs — read from <img> tags inside this card
        img_els = card.query_selector_all("img")
        # The Vue app sets images via src; also collect the img-count badge
        images = [el.get_attribute("src") for el in img_els if el.get_attribute("src")]

        listing_id = attr("listing-id")
        if not listing_id:
            continue

        try:
            price   = int(attr("price"))
            mileage = int(attr("mileage"))
            year    = int(attr("year"))
        except ValueError:
            price = mileage = year = 0

        results.append({
            "id":           listing_id,
            "make":         attr("make"),
            "model":        attr("model"),
            "year":         year,
            "price":        price,
            "mileage":      mileage,
            "transmission": attr("transmission"),
            "fuel":         attr("fuel"),
            "color":        attr("color"),
            "dealer":       attr("dealer"),
            "location":     attr("location"),
            "listed":       attr("listed"),
            "images":       images,
            "scraped_at":   datetime.now(timezone.utc).isoformat(),
        })

    return results


# ---------------------------------------------------------------------------
# Page navigation with retry
# ---------------------------------------------------------------------------

def first_listing_id(page: Page) -> str:
    """Return the first listing id currently shown in the grid, if any."""
    first_card = page.query_selector("[data-listing-id]")
    if not first_card:
        return ""
    return first_card.get_attribute("data-listing-id") or ""

def click_page_button(page: Page, page_num: int) -> bool:
    """
    Click the pagination button for `page_num`.
    The Vue app shows a 280 ms loading overlay — we wait for it to clear.
    Returns True on success.
    """
    previous_first_id = first_listing_id(page)

    for btn in page.query_selector_all(".pagination .page-btn"):
        if btn.inner_text().strip() == str(page_num):
            btn.click()
            # Overlay may appear briefly; if it does, wait for it to disappear.
            try:
                page.wait_for_selector(".loading-overlay", state="visible", timeout=1500)
            except PWTimeout:
                pass
            # Wait for loading overlay to disappear
            try:
                page.wait_for_selector(".loading-overlay", state="detached", timeout=5000)
            except PWTimeout:
                pass  # overlay may be too fast to catch
            # Wait until the new page is active and card data has changed.
            page.wait_for_function(
                """([expectedPage, prevId]) => {
                    const active = document.querySelector('.pagination .page-btn.active');
                    if (!active || active.textContent.trim() !== String(expectedPage)) {
                        return false;
                    }
                    const cards = document.querySelectorAll('[data-listing-id]');
                    if (!cards.length) {
                        return false;
                    }
                    const firstId = cards[0].getAttribute('data-listing-id') || '';
                    return !prevId || firstId !== prevId;
                }""",
                arg=[page_num, previous_first_id],
                timeout=PAGE_TIMEOUT,
            )
            time.sleep(0.3)  # small buffer for Vue reactivity
            return True

    log.warning("Pagination button for page %d not found.", page_num)
    return False


def should_use_proxy(target_url: str) -> bool:
    """Disable proxies for local targets where external proxies cannot route."""
    host = (urlparse(target_url).hostname or "").lower()
    return host not in {"localhost", "127.0.0.1", "::1"}


# ---------------------------------------------------------------------------
# Core scrape loop
# ---------------------------------------------------------------------------

def scrape(
    proxy_manager: ProxyManager,
    max_pages: Optional[int] = None,
    skip_seen: bool = True,
) -> list[dict]:
    seen_ids = load_seen_ids() if skip_seen else set()
    all_listings: list[dict] = []
    run_new    = 0
    run_dupes  = 0

    with sync_playwright() as pw:
        ua      = random.choice(USER_AGENTS)
        use_proxy = should_use_proxy(BASE_URL)
        proxy   = proxy_manager.get() if use_proxy else {}

        launch_kwargs = {
            "headless": HEADLESS,
            "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        }
        # Playwright proxy format: {"server": "http://..."}
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy.get("http", "")}
        elif not use_proxy:
            log.info("Target is local; bypassing proxy routing.")

        browser = pw.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent=ua,
            viewport={"width": 1440, "height": 900},
            extra_http_headers={
                "Accept-Language": "en-ZA,en;q=0.9",
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        page = context.new_page()

        try:
            # ── Initial load ──────────────────────────────────────────────
            log.info("Navigating to %s  [UA: ...%s]", BASE_URL, ua[-30:])
            attempt = 0
            while attempt < MAX_RETRIES:
                try:
                    page.goto(BASE_URL, wait_until="networkidle", timeout=PAGE_TIMEOUT)
                    page.wait_for_selector("[data-listing-id]", timeout=PAGE_TIMEOUT)
                    proxy_manager.report_success(proxy)
                    break
                except (PWTimeout, PWError) as exc:
                    attempt += 1
                    proxy_manager.report_failure(proxy)
                    delay = RETRY_DELAY * (2 ** (attempt - 1))
                    log.warning(
                        "Page load failed (attempt %d/%d): %s — retrying in %.1fs…",
                        attempt,
                        MAX_RETRIES,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                    proxy = proxy_manager.get() if use_proxy else {}
            else:
                log.error("Failed to load %s after %d attempts.", BASE_URL, MAX_RETRIES)
                return []

            # ── Discover total pages ───────────────────────────────────────
            # Read from the toolbar text: "Page X of Y"
            toolbar_text = page.inner_text(".toolbar") if page.query_selector(".toolbar") else ""
            m = re.search(r"Page\s+\d+\s+of\s+(\d+)", toolbar_text)
            total_pages = int(m.group(1)) if m else 1
            if max_pages:
                total_pages = min(total_pages, max_pages)

            log.info("Found %d pages to scrape.", total_pages)

            # ── Paginate ──────────────────────────────────────────────────
            for p in range(1, total_pages + 1):
                log.info("── Page %d / %d ──", p, total_pages)

                if p > 1:
                    ok = click_page_button(page, p)
                    if not ok:
                        log.warning("Skipping page %d — button not found.", p)
                        continue

                cards = extract_cards(page)
                page_new   = 0
                page_dupes = 0

                for card in cards:
                    if card["id"] in seen_ids:
                        page_dupes += 1
                        run_dupes  += 1
                    else:
                        seen_ids.add(card["id"])
                        all_listings.append(card)
                        page_new += 1
                        run_new  += 1

                log.info(
                    "  Extracted %d cards — %d new, %d duplicate%s skipped.",
                    len(cards), page_new, page_dupes, "s" if page_dupes != 1 else "",
                )

                # Polite crawl delay
                time.sleep(random.uniform(0.8, 1.6))

        except KeyboardInterrupt:
            log.info("Interrupted by user — saving partial results…")
        finally:
            browser.close()

    log.info(
        "Scrape complete — %d new listings collected, %d duplicates skipped.",
        run_new, run_dupes,
    )
    return all_listings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AutoCars demo scraper")
    parser.add_argument("--max-pages",  type=int, default=None,  help="Limit pages scraped (default: all)")
    parser.add_argument("--no-dedup",   action="store_true",     help="Disable duplicate check against store")
    parser.add_argument("--visible",    action="store_true",     help="Run browser in headed (visible) mode")
    args = parser.parse_args()

    global HEADLESS
    if args.visible:
        HEADLESS = False

    log.info("=" * 60)
    log.info("AutoCars Scraper  |  %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("Target: %s", BASE_URL)
    log.info("=" * 60)

    proxy_manager = ProxyManager()
    log.info("Proxy pool: %d healthy proxies available.", proxy_manager.healthy_count)

    listings = scrape(
        proxy_manager  = proxy_manager,
        max_pages      = args.max_pages,
        skip_seen      = not args.no_dedup,
    )

    if not listings:
        log.warning("No listings collected — nothing to save.")
        return

    # Save run file
    run_file = save_run_file(listings)

    # Update cumulative store
    new_items = save_to_store(listings)

    # Print summary table
    print("\n" + "=" * 60)
    print(f"  Run summary")
    print("=" * 60)
    print(f"  Total collected  : {len(listings)}")
    print(f"  New to store     : {len(new_items)}")
    print(f"  Run file         : {run_file}")
    print(f"  Cumulative store : {STORE_FILE}")
    print("=" * 60)

    # Print first 3 records as a preview
    print("\n  Sample records:\n")
    for item in listings[:3]:
        print(f"  [{item['id']}] {item['year']} {item['make']} {item['model']}")
        print(f"    Price: R {item['price']:,}  |  {item['mileage']:,} km  |  {item['fuel']}  |  {item['transmission']}")
        print(f"    Dealer: {item['dealer']}, {item['location']}")
        print(f"    Images: {len(item['images'])}  |  Listed: {item['listed']}")
        print()


if __name__ == "__main__":
    main()