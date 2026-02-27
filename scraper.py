"""
Expo West 2026 Exhibitor Scraper — main orchestrator.

Data sources (as confirmed):
  Listing  : https://www.expowest.com/en/exhibitor-list/2026-exhibitor-list.html
  Detail   : https://attend.expowest.com/widget/event/natural-products-expo-west-2026/exhibitor/{id}

attend.expowest.com is Swapcard (white-labelled).  When the widget loads it
makes GraphQL requests; the scraper intercepts those responses to get clean
structured JSON (name, booth, hall, country, state, categories, social links,
company URL, team members) without fragile HTML scraping.  HTML parsing is
kept as a fallback.

Usage:
    python scraper.py                   # normal run
    python scraper.py --reset           # clear checkpoints and start fresh
    python scraper.py --list-only       # only discover exhibitor URLs, then stop
    python scraper.py --headful         # show the browser window (debug)
    python scraper.py --limit 20        # scrape only the first N exhibitors
"""
import argparse
import asyncio
import json
import logging
import sys

from playwright.async_api import async_playwright, Page

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
    parser.add_argument("--reset", action="store_true",
                        help="Clear all checkpoints and start fresh.")
    parser.add_argument("--list-only", action="store_true",
                        help="Discover exhibitor URLs only; skip detail scraping.")
    parser.add_argument("--headful", action="store_true",
                        help="Show the browser window (useful for debugging).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Scrape at most N exhibitors (0 = unlimited).")
    return parser.parse_args()


# ── Phase 1: Discover exhibitor links ─────────────────────────────────────────

async def discover_exhibitor_links(
    page: Page,
    links_cache: ExhibitorLinkCache,
    progress: ProgressTracker,
) -> list[dict]:
    """
    Collect every exhibitor detail URL.

    Strategy (in priority order):
      1. Navigate to the official expowest.com listing page.
         Intercept ALL JSON / GraphQL responses that carry exhibitor data.
         Also parse <a href> elements that link to attend.expowest.com/exhibitor/.
      2. If that yields nothing (the listing page uses an iframe or other embed),
         navigate directly to the Swapcard event widget page and paginate.
      3. Continue paginating until 3 consecutive pages yield no new links.
    """
    logger.info("=== Phase 1: Discovering exhibitor links ===")

    all_links: list[dict] = []
    all_keys: set[str] = set()
    ajax_slugs: list[str] = []       # collected via AJAX/JSON interception

    # ── Intercept ALL JSON responses throughout Phase 1 ────────────────────────
    async def handle_response(response):
        try:
            content_type = response.headers.get("content-type", "")
            if "json" in content_type:
                body = await response.json()
                # Try to extract exhibitor list data from GraphQL responses
                found = extractors.parse_graphql_exhibitor_list(body)
                for link in found:
                    key = link.get("slug") or link["url"]
                    if key not in all_keys:
                        all_keys.add(key)
                        all_links.append(link)
                        ajax_slugs.append(key)
        except Exception:
            pass

    page.on("response", handle_response)

    def harvest_dom(html: str, base_url: str = "") -> int:
        """Add links found via DOM parsing; return count of new links."""
        new = 0
        for link in extractors.extract_exhibitor_links(html, base_url):
            key = link.get("slug") or link["url"]
            if key not in all_keys:
                all_keys.add(key)
                all_links.append(link)
                new += 1
        return new

    # ── Step 1: Official expowest.com listing ──────────────────────────────────
    logger.info(f"Navigating to listing page: {config.EXHIBITOR_LIST_URL}")
    ok = await safe_navigate(page, config.EXHIBITOR_LIST_URL)
    if ok:
        await wait_for_network_idle(page)
        await asyncio.sleep(3)   # let lazy-loaded content settle
        html = await page.content()
        n_dom = harvest_dom(html, config.EXHIBITOR_LIST_URL)
        logger.info(
            f"expowest.com listing — DOM: {n_dom} links, "
            f"AJAX so far: {len(ajax_slugs)}"
        )

    # ── Step 2: If we still have few/no links, use Swapcard widget ────────────
    if len(all_links) < 10:
        logger.info(
            f"Few links from expowest.com ({len(all_links)}); "
            f"falling back to Swapcard event widget: {config.EVENT_WIDGET_URL}"
        )
        ok = await safe_navigate(page, config.EVENT_WIDGET_URL)
        if not ok:
            logger.error("Could not load Swapcard event widget. Aborting Phase 1.")
            return []

        cards_found = await wait_for_exhibitor_cards(page)
        if not cards_found:
            logger.warning("No exhibitor cards detected on Swapcard widget page.")
            await screenshot_on_error(page, "swapcard_no_cards")

        await wait_for_network_idle(page)
        await asyncio.sleep(3)
        html = await page.content()

        total_pages = extractors.extract_total_pages(html)
        logger.info(f"Swapcard widget: DOM detected {total_pages} page(s).")
        progress.update(total_pages=total_pages, phase="listing")

        n = harvest_dom(html, config.BASE_URL)
        logger.info(f"Swapcard widget page 1: +{n} links (total: {len(all_links)})")

        consecutive_empty = 0
        page_num = 2

        while True:
            if page_num > total_pages and consecutive_empty >= 3:
                logger.info(
                    f"3 consecutive empty pages past detected total ({total_pages}). "
                    f"Discovery complete."
                )
                break

            progress.set("current_page", page_num)
            await _navigate_to_listing_page(page, page_num)

            cards_found = await wait_for_exhibitor_cards(page)
            if not cards_found:
                logger.warning(f"No cards on page {page_num}.")
                await screenshot_on_error(page, f"listing_page_{page_num}")
                consecutive_empty += 1
                page_num += 1
                continue

            await wait_for_network_idle(page)
            await asyncio.sleep(2)
            html = await page.content()

            n = harvest_dom(html, config.BASE_URL)
            if n == 0:
                consecutive_empty += 1
                logger.info(
                    f"Page {page_num}: no new links "
                    f"(empty streak: {consecutive_empty})"
                )
            else:
                consecutive_empty = 0
                if page_num >= total_pages:
                    total_pages = page_num + 1
                logger.info(
                    f"Page {page_num}: +{n} links (total: {len(all_links)})"
                )

            page_num += 1
            await random_delay()

    logger.info(f"Phase 1 complete: {len(all_links)} unique exhibitors discovered.")
    links_cache.set(all_links)
    return all_links


async def _navigate_to_listing_page(page: Page, page_num: int) -> None:
    """Navigate to a specific page of the Swapcard event widget."""
    # Strategy 1: Click numbered pagination link
    try:
        selector = (
            f"[data-page='{page_num}'], "
            f".pagination a:has-text('{page_num}'), "
            f"button:has-text('{page_num}')"
        )
        link = page.locator(selector).first
        if await link.count() > 0:
            await link.click()
            await page.wait_for_load_state("domcontentloaded")
            return
    except Exception:
        pass

    # Strategy 2: URL param (works for some Swapcard / event sites)
    for url_template in [
        f"{config.EVENT_WIDGET_URL}?page={page_num}",
        f"{config.EXHIBITOR_LIST_URL}?page={page_num}",
    ]:
        logger.debug(f"Paginating via URL: {url_template}")
        ok = await safe_navigate(page, url_template)
        if ok:
            return


# ── Team-tab helper ───────────────────────────────────────────────────────────

async def _get_team_tab_html(page: Page) -> str:
    """
    Try to click the Team / People / Staff tab on an exhibitor detail page so
    that AJAX-rendered content is present in the DOM.
    Returns the full page HTML after the attempt.
    """
    team_tab_selectors = [
        "a[href*='_team']", "a[href*='_people']", "a[href*='_staff']",
        "[data-tab='team']", "[data-target*='team']", "[href='#team']",
        "a:has-text('Team')", "a:has-text('People')", "a:has-text('Staff')",
        "li:has-text('Team') a", "li:has-text('People') a",
        "button:has-text('Team')", "button:has-text('People')",
        # Swapcard tab patterns
        "[class*='tab' i]:has-text('People')",
        "[class*='tab' i]:has-text('Team')",
    ]
    for sel in team_tab_selectors:
        try:
            tab = page.locator(sel).first
            if await tab.count() > 0:
                await tab.click()
                await asyncio.sleep(2)   # let AJAX render
                break
        except Exception:
            pass
    return await page.content()


# ── Phase 2: Scrape detail pages ───────────────────────────────────────────────

async def scrape_detail_pages(
    page: Page,
    links: list[dict],
    scraped_cache: ScrapedCache,
    team_cache: TeamMembersCache,
    limit: int = 0,
) -> list[dict]:
    """
    Visit each exhibitor detail page, extract data, save incrementally.

    For each URL the scraper:
      1. Intercepts the Swapcard GraphQL response for structured JSON data.
      2. Falls back to embedded <script> JSON extraction.
      3. Falls back to HTML parsing.
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
        if limit and new_count >= limit:
            logger.info(f"--limit {limit} reached; stopping.")
            break

        key = link.get("slug") or link.get("booth_id") or link["url"]
        if scraped_cache.has(key):
            continue

        url = link["url"]
        logger.info(f"[{idx}/{total}] Scraping: {url}")

        try:
            record, team_members = await _scrape_one_exhibitor(page, url)
        except Exception as exc:
            logger.error(f"Failed to scrape {url}: {exc}", exc_info=True)
            await screenshot_on_error(page, f"detail_{key[:30]}")
            record = {"exhibitor_name": key, "source_url": url, "_error": str(exc)}
            team_members = []

        if team_members:
            team_cache.extend(team_members)
            logger.debug(f"  → {len(team_members)} team member(s)")

        scraped_cache.mark_done(key, record)
        exhibitor_records.append(record)
        new_count += 1

        if new_count % config.CHECKPOINT_INTERVAL == 0:
            logger.info(f"Checkpoint save at {len(exhibitor_records)} records...")
            _safe_write_outputs(exhibitor_records, team_cache.all_records())

        await random_delay()

    logger.info(f"Phase 2 complete: {new_count} new exhibitors scraped.")
    return exhibitor_records


async def _scrape_one_exhibitor(page: Page, url: str) -> tuple[dict, list[dict]]:
    """
    Navigate to a detail page and extract all exhibitor data.

    Returns (record_dict, team_members_list).
    Prefers GraphQL JSON interception over HTML parsing.
    Retries up to MAX_RETRIES on failure.
    """
    last_exc = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            # ── Set up GraphQL interception BEFORE navigating ──────────────────
            graphql_responses: list[dict] = []

            async def capture_graphql(response):
                try:
                    ct = response.headers.get("content-type", "")
                    if "json" in ct:
                        body = await response.json()
                        if isinstance(body, dict) and "data" in body:
                            graphql_responses.append(body)
                except Exception:
                    pass

            page.on("response", capture_graphql)

            ok = await safe_navigate(page, url)
            if not ok:
                raise RuntimeError(f"Navigation failed for {url}")

            # Wait for Swapcard React app to finish its initial data fetch
            await wait_for_network_idle(page)
            await asyncio.sleep(2)

            # ── Attempt 1: GraphQL JSON ────────────────────────────────────────
            record: dict | None = None
            team_members: list[dict] = []

            exhibitor_name = ""
            for body in graphql_responses:
                r = extractors.parse_graphql_exhibitor(body, url)
                if r and r.get("exhibitor_name"):
                    record = r
                    exhibitor_name = r["exhibitor_name"]
                    # Also try extracting team members from same response
                    tm = extractors.parse_graphql_team_members(body, exhibitor_name)
                    if tm:
                        team_members = tm
                    break

            # ── Attempt 2: Embedded <script> JSON ─────────────────────────────
            if not record or not record.get("exhibitor_name"):
                html = await page.content()
                for embedded in extractors.extract_embedded_json(html):
                    r = extractors.parse_graphql_exhibitor(embedded, url)
                    if r and r.get("exhibitor_name"):
                        record = r
                        exhibitor_name = r["exhibitor_name"]
                        break

            # ── Attempt 3: HTML parsing ────────────────────────────────────────
            if not record or not record.get("exhibitor_name"):
                html = await page.content()
                record = extractors.extract_exhibitor_detail(html, url)
                exhibitor_name = record.get("exhibitor_name", "")

            # ── Team members: click team tab, then HTML or GraphQL ─────────────
            if not team_members:
                team_html = await _get_team_tab_html(page)
                # Check for any new GraphQL responses triggered by tab click
                for body in graphql_responses:
                    tm = extractors.parse_graphql_team_members(body, exhibitor_name)
                    if tm:
                        team_members = tm
                        break
                # Fall back to HTML
                if not team_members:
                    team_members = extractors.extract_team_members(
                        team_html, exhibitor_name
                    )

            # Remove response listener before next navigation
            try:
                page.remove_listener("response", capture_graphql)
            except Exception:
                pass

            return record, team_members

        except Exception as exc:
            last_exc = exc
            try:
                page.remove_listener("response", capture_graphql)
            except Exception:
                pass
            if attempt < config.MAX_RETRIES:
                wait = config.RETRY_BASE_WAIT_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    f"Attempt {attempt} failed for {url}: {exc}. "
                    f"Retrying in {wait:.0f}s…"
                )
                await asyncio.sleep(wait)

    raise RuntimeError(
        f"All {config.MAX_RETRIES} attempts failed for {url}"
    ) from last_exc


# ── Phase 3: Write output ──────────────────────────────────────────────────────

def _safe_write_outputs(exhibitor_records: list[dict], team_records: list[dict]) -> None:
    try:
        output.write_exhibitors_excel(exhibitor_records, config.EXHIBITORS_OUTPUT)
    except Exception as exc:
        logger.error(f"Could not write exhibitors Excel: {exc}")
    try:
        output.write_team_members_excel(team_records, config.TEAM_MEMBERS_OUTPUT)
    except Exception as exc:
        logger.error(f"Could not write team members Excel: {exc}")


# ── Browser factory ────────────────────────────────────────────────────────────

async def create_browser_context(playwright, headless: bool):
    user_agent = get_random_user_agent()
    browser = await playwright.chromium.launch(
        headless=headless,
        args=config.BROWSER_ARGS,
    )
    context = await browser.new_context(
        user_agent=user_agent,
        viewport={"width": 1440, "height": 900},
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
    logger.debug(f"Browser launched (headless={headless}, UA={user_agent[:60]}…)")
    return browser, context


# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> int:
    args = parse_args()
    headless = config.HEADLESS and not args.headful

    if args.reset:
        logger.info("--reset: clearing all checkpoint files…")
        ExhibitorLinkCache().clear()
        ScrapedCache().clear()
        TeamMembersCache().clear()
        ProgressTracker().clear()

    links_cache  = ExhibitorLinkCache()
    scraped_cache = ScrapedCache()
    team_cache   = TeamMembersCache()
    progress     = ProgressTracker()

    async with async_playwright() as playwright:
        browser, context = await create_browser_context(playwright, headless=headless)
        page = await context.new_page()

        try:
            # Phase 1
            if links_cache.is_populated():
                logger.info(
                    f"Loaded {len(links_cache.links)} exhibitor links from "
                    f"checkpoint (skipping Phase 1)."
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

            # Phase 2
            exhibitor_records = await scrape_detail_pages(
                page, links, scraped_cache, team_cache, limit=args.limit
            )

            # Phase 3
            logger.info("=== Phase 3: Writing final output files ===")
            _safe_write_outputs(exhibitor_records, team_cache.all_records())

            logger.info(
                f"Done. {len(exhibitor_records)} exhibitors → "
                f"{config.EXHIBITORS_OUTPUT}\n"
                f"       {team_cache.count()} team members → "
                f"{config.TEAM_MEMBERS_OUTPUT}"
            )
            return 0

        except KeyboardInterrupt:
            logger.info("Interrupted. Saving partial output…")
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
