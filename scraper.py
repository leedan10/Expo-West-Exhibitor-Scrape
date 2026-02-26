"""
Expo West 2026 Exhibitor Scraper — main orchestrator.

Usage:
    python scraper.py                   # normal run
    python scraper.py --reset           # clear checkpoints and start fresh
    python scraper.py --list-only       # only crawl the listing page, then stop
    python scraper.py --headful         # run with a visible browser window

Workflow:
    Phase 1 — Discover all exhibitor URLs from the listing page (with pagination).
    Phase 2 — Visit each exhibitor detail page; extract and save data.
    Phase 3 — Write final Excel files.

Crash recovery:
    Checkpoint files in checkpoints/ track which exhibitors have been scraped.
    Re-run the script to resume from where it left off.
"""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext

import config
import extractors
import output
from checkpoint import (
    ExhibitorLinkCache,
    ScrapedCache,
    TeamMembersCache,
    ProgressTracker,
)
from utils import (
    logger,
    random_delay,
    get_random_user_agent,
    screenshot_on_error,
    wait_for_exhibitor_cards,
    wait_for_network_idle,
    safe_navigate,
)

# ── Argument parsing ───────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Expo West 2026 Exhibitor Scraper")
    parser.add_argument(
        "--reset", action="store_true",
        help="Clear all checkpoints and start a fresh scrape.",
    )
    parser.add_argument(
        "--list-only", action="store_true",
        help="Only crawl the listing page to discover exhibitor URLs; do not scrape detail pages.",
    )
    parser.add_argument(
        "--headful", action="store_true",
        help="Run the browser in non-headless mode (useful for debugging).",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Scrape at most N exhibitors (0 = unlimited). Useful for testing.",
    )
    return parser.parse_args()


# ── Phase 1: Discover exhibitor links ─────────────────────────────────────────

async def discover_exhibitor_links(
    page: Page,
    links_cache: ExhibitorLinkCache,
    progress: ProgressTracker,
) -> list[dict]:
    """
    Navigate to the exhibitor listing page, wait for AJAX cards, paginate
    through all pages, and return the full list of exhibitor link dicts.

    Strategy:
      1. Intercept XHR/fetch calls to capture JSON from WordPress admin-ajax.php.
         If successful, parse ALL exhibitors from the JSON response directly.
      2. Fallback: detect pagination via DOM, iterate pages clicking Next.
    """
    logger.info("=== Phase 1: Discovering exhibitor links ===")
    all_links: list[dict] = []
    intercepted_slugs: list[str] = []

    # ── Set up network interception for AJAX JSON ──────────────────────────────
    async def handle_response(response):
        url = response.url
        if "admin-ajax.php" in url or "exhibitors" in url.lower():
            try:
                content_type = response.headers.get("content-type", "")
                if "json" in content_type:
                    body = await response.json()
                    _parse_ajax_json(body, intercepted_slugs)
            except Exception:
                pass  # Not every XHR is the one we want

    page.on("response", handle_response)

    logger.info(f"Navigating to {config.EXHIBITOR_LIST_URL}")
    ok = await safe_navigate(page, config.EXHIBITOR_LIST_URL)
    if not ok:
        logger.error("Could not load the exhibitor listing page. Aborting Phase 1.")
        return []

    cards_found = await wait_for_exhibitor_cards(page)
    if not cards_found:
        logger.warning("No exhibitor cards detected after page load — the page may require interaction.")
        await screenshot_on_error(page, "listing_no_cards")

    await wait_for_network_idle(page)

    # If AJAX interception caught slugs, use those
    if intercepted_slugs:
        logger.info(f"AJAX interception captured {len(intercepted_slugs)} slugs.")
        all_links = [
            {"url": f"{config.BASE_URL}/co/{slug}", "slug": slug, "booth_id": None}
            for slug in intercepted_slugs
        ]
        links_cache.set(all_links)
        return all_links

    # ── DOM-based pagination fallback ──────────────────────────────────────────
    logger.info("AJAX interception did not capture data; falling back to DOM pagination.")

    # Detect total pages
    html = await page.content()
    total_pages = extractors.extract_total_pages(html)
    logger.info(f"Detected {total_pages} total page(s) in the listing.")
    progress.update(total_pages=total_pages, phase="listing")

    # Page 1 links
    page_links = extractors.extract_exhibitor_links(html)
    all_links.extend(page_links)
    logger.info(f"Page 1: found {len(page_links)} links (running total: {len(all_links)})")

    # Pages 2..N
    for page_num in range(2, total_pages + 1):
        progress.set("current_page", page_num)
        await _navigate_to_listing_page(page, page_num)
        cards_found = await wait_for_exhibitor_cards(page)
        if not cards_found:
            logger.warning(f"No cards found on page {page_num}; skipping.")
            await screenshot_on_error(page, f"listing_page_{page_num}")
            continue
        await wait_for_network_idle(page)
        html = await page.content()
        page_links = extractors.extract_exhibitor_links(html)
        all_links.extend(page_links)
        logger.info(f"Page {page_num}: +{len(page_links)} links (running total: {len(all_links)})")
        await random_delay()

    # Deduplicate (preserve order)
    seen: set[str] = set()
    deduped: list[dict] = []
    for link in all_links:
        key = link["url"]
        if key not in seen:
            seen.add(key)
            deduped.append(link)
    logger.info(f"Phase 1 complete: {len(deduped)} unique exhibitors discovered.")
    links_cache.set(deduped)
    return deduped


def _parse_ajax_json(body, result_slugs: list[str]) -> None:
    """
    Attempt to extract exhibitor slugs from a WordPress AJAX JSON response.
    The structure varies; this tries common SmallWorld Labs patterns.
    """
    import re

    def walk(obj):
        if isinstance(obj, dict):
            # Look for slug/url fields
            for key in ("slug", "company_slug", "url", "permalink"):
                val = obj.get(key, "")
                if isinstance(val, str):
                    m = re.search(r"/co/([^/?#\"]+)", val)
                    if m and m.group(1) not in result_slugs:
                        result_slugs.append(m.group(1))
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(body)


async def _navigate_to_listing_page(page: Page, page_num: int) -> None:
    """
    Navigate to a specific page of the exhibitor listing.
    Tries: clicking a numbered pagination link, then URL param fallback.
    """
    # Strategy 1: Click the numbered pagination link in the DOM
    try:
        selector = f"[data-page='{page_num}'], .pagination a:has-text('{page_num}')"
        link = page.locator(selector).first
        if await link.count() > 0:
            await link.click()
            await page.wait_for_load_state("domcontentloaded")
            return
    except Exception:
        pass

    # Strategy 2: Navigate via URL query parameter
    url = f"{config.EXHIBITOR_LIST_URL}?page={page_num}"
    logger.debug(f"Clicking pagination failed; navigating directly to {url}")
    await safe_navigate(page, url)


# ── Phase 2: Scrape detail pages ───────────────────────────────────────────────

async def scrape_detail_pages(
    page: Page,
    links: list[dict],
    scraped_cache: ScrapedCache,
    team_cache: TeamMembersCache,
    limit: int = 0,
) -> list[dict]:
    """
    Visit each exhibitor detail page and extract data.
    Skips already-scraped exhibitors (resume from checkpoint).
    Writes partial Excel output every CHECKPOINT_INTERVAL records.
    """
    logger.info("=== Phase 2: Scraping exhibitor detail pages ===")
    exhibitor_records: list[dict] = scraped_cache.all_records()
    scraped_count = scraped_cache.count()
    total = len(links)

    logger.info(
        f"{scraped_count}/{total} already scraped (loaded from checkpoint). "
        f"Resuming from exhibitor #{scraped_count + 1}."
    )

    new_count = 0
    for idx, link in enumerate(links, start=1):
        # Apply limit (for testing)
        if limit and new_count >= limit:
            logger.info(f"--limit {limit} reached; stopping.")
            break

        key = link.get("slug") or link.get("booth_id") or link["url"]
        if scraped_cache.has(key):
            continue

        url = link["url"]
        logger.info(f"[{idx}/{total}] Scraping: {url}")

        try:
            record = await _scrape_one_exhibitor(page, url)
        except Exception as exc:
            logger.error(f"Failed to scrape {url}: {exc}", exc_info=True)
            await screenshot_on_error(page, f"detail_{key[:30]}")
            # Store a partial record so we don't retry endlessly
            record = {"exhibitor_name": key, "source_url": url, "_error": str(exc)}

        # Extract team members from the same page HTML (already loaded)
        exhibitor_name = record.get("exhibitor_name", key)
        try:
            html = await page.content()
            team_members = extractors.extract_team_members(html, exhibitor_name)
            if team_members:
                team_cache.extend(team_members)
                logger.debug(f"  → {len(team_members)} team member(s) found")
        except Exception as exc:
            logger.warning(f"Team member extraction failed for {url}: {exc}")

        scraped_cache.mark_done(key, record)
        exhibitor_records.append(record)
        new_count += 1

        # Periodic checkpoint save of Excel files
        if new_count % config.CHECKPOINT_INTERVAL == 0:
            logger.info(f"Checkpoint save at {len(exhibitor_records)} total records...")
            _safe_write_outputs(exhibitor_records, team_cache.all_records())

        await random_delay()

    logger.info(f"Phase 2 complete: {new_count} new exhibitors scraped.")
    return exhibitor_records


async def _scrape_one_exhibitor(page: Page, url: str) -> dict:
    """Navigate to a detail page and extract exhibitor data. Retries up to MAX_RETRIES."""
    last_exc = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            ok = await safe_navigate(page, url)
            if not ok:
                raise RuntimeError(f"Navigation returned False for {url}")
            await wait_for_network_idle(page)
            html = await page.content()
            return extractors.extract_exhibitor_detail(html, url)
        except Exception as exc:
            last_exc = exc
            if attempt < config.MAX_RETRIES:
                wait = config.RETRY_BASE_WAIT_SECONDS * (2 ** (attempt - 1))
                logger.warning(f"Attempt {attempt} failed for {url}: {exc}. Retrying in {wait:.0f}s...")
                await asyncio.sleep(wait)
    raise RuntimeError(f"All {config.MAX_RETRIES} attempts failed for {url}") from last_exc


# ── Phase 3: Write final output ────────────────────────────────────────────────

def _safe_write_outputs(exhibitor_records: list[dict], team_records: list[dict]) -> None:
    """Write Excel files, logging but not raising on failure."""
    try:
        output.write_exhibitors_excel(exhibitor_records, config.EXHIBITORS_OUTPUT)
    except Exception as exc:
        logger.error(f"Could not write exhibitors Excel: {exc}")
    try:
        output.write_team_members_excel(team_records, config.TEAM_MEMBERS_OUTPUT)
    except Exception as exc:
        logger.error(f"Could not write team members Excel: {exc}")


# ── New browser context factory ────────────────────────────────────────────────

async def create_browser_context(playwright, headless: bool):
    """Launch chromium with anti-detection settings and a realistic user-agent."""
    user_agent = get_random_user_agent()
    browser = await playwright.chromium.launch(
        headless=headless,
        args=config.BROWSER_ARGS,
    )
    context = await browser.new_context(
        user_agent=user_agent,
        viewport={"width": 1366, "height": 768},
        locale="en-US",
        timezone_id="America/Los_Angeles",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
        },
    )
    logger.debug(f"Browser launched (headless={headless}, UA={user_agent[:60]}...)")
    return browser, context


# ── Main entry point ───────────────────────────────────────────────────────────

async def main() -> int:
    """
    Orchestrates all three phases. Returns exit code (0=success, 1=error).
    """
    args = parse_args()

    # Override headless from CLI flag
    headless = config.HEADLESS and not args.headful

    # Reset checkpoints if requested
    if args.reset:
        logger.info("--reset: clearing all checkpoint files...")
        ExhibitorLinkCache().clear()
        ScrapedCache().clear()
        TeamMembersCache().clear()
        ProgressTracker().clear()

    links_cache = ExhibitorLinkCache()
    scraped_cache = ScrapedCache()
    team_cache = TeamMembersCache()
    progress = ProgressTracker()

    async with async_playwright() as playwright:
        browser, context = await create_browser_context(playwright, headless=headless)
        page = await context.new_page()

        try:
            # ── Phase 1 ────────────────────────────────────────────────────────
            if links_cache.is_populated():
                logger.info(
                    f"Loaded {len(links_cache.links)} exhibitor links from checkpoint "
                    f"(skip Phase 1)."
                )
                links = links_cache.links
            else:
                links = await discover_exhibitor_links(page, links_cache, progress)

            if not links:
                logger.error("No exhibitor links found. Cannot proceed.")
                return 1

            if args.list_only:
                logger.info(f"--list-only: discovered {len(links)} links. Exiting.")
                return 0

            # ── Phase 2 ────────────────────────────────────────────────────────
            exhibitor_records = await scrape_detail_pages(
                page, links, scraped_cache, team_cache, limit=args.limit
            )

            # ── Phase 3 ────────────────────────────────────────────────────────
            logger.info("=== Phase 3: Writing final output files ===")
            _safe_write_outputs(exhibitor_records, team_cache.all_records())

            logger.info(
                f"Done. {len(exhibitor_records)} exhibitors → {config.EXHIBITORS_OUTPUT}\n"
                f"       {team_cache.count()} team members → {config.TEAM_MEMBERS_OUTPUT}"
            )
            return 0

        except KeyboardInterrupt:
            logger.info("Interrupted by user. Saving partial output...")
            _safe_write_outputs(scraped_cache.all_records(), team_cache.all_records())
            return 1

        except Exception as exc:
            logger.critical(f"Unhandled exception: {exc}", exc_info=True)
            await screenshot_on_error(page, "fatal_error")
            _safe_write_outputs(scraped_cache.all_records(), team_cache.all_records())
            return 1

        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
