"""
Shared utilities: logging setup, random delays, retry decorator,
user-agent rotation, and screenshot helper.
"""
import asyncio
import logging
import random
import time
from pathlib import Path

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

import config

# ── Logger setup ───────────────────────────────────────────────────────────────

def setup_logging() -> logging.Logger:
    """
    Configure root logger to write to both console and rotating log file.
    Returns the named scraper logger.
    """
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
    ]

    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL),
        format=log_format,
        datefmt=date_format,
        handlers=handlers,
        force=True,
    )
    return logging.getLogger("expowest_scraper")


logger = setup_logging()


# ── Delay helpers ──────────────────────────────────────────────────────────────

async def random_delay(
    min_seconds: float = config.MIN_DELAY_SECONDS,
    max_seconds: float = config.MAX_DELAY_SECONDS,
) -> None:
    """Async sleep for a random duration within [min, max] seconds."""
    delay = random.uniform(min_seconds, max_seconds)
    logger.debug(f"Sleeping {delay:.2f}s")
    await asyncio.sleep(delay)


# ── User-agent rotation ────────────────────────────────────────────────────────

def get_random_user_agent() -> str:
    """
    Return a random browser user-agent string.
    Tries fake-useragent first; falls back to the static pool in config.py.
    """
    try:
        from fake_useragent import UserAgent
        ua = UserAgent()
        return ua.chrome
    except Exception as exc:
        logger.warning(f"fake-useragent failed ({exc}), using static pool")
        return random.choice(config.USER_AGENTS)


# ── Screenshot helper ──────────────────────────────────────────────────────────

async def screenshot_on_error(page, label: str) -> Path:
    """
    Take a full-page screenshot and save it to screenshots/.
    label should be a short slug describing the error context.
    Returns the Path to the saved file.
    """
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    timestamp = int(time.time())
    path = config.SCREENSHOT_DIR / f"error_{safe_label}_{timestamp}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
        logger.info(f"Screenshot saved: {path}")
    except Exception as exc:
        logger.error(f"Could not take screenshot: {exc}")
    return path


# ── Retry decorator ────────────────────────────────────────────────────────────

def make_retry_decorator(max_attempts: int = config.MAX_RETRIES):
    """
    Return a tenacity @retry decorator with exponential backoff.

    Usage:
        @make_retry_decorator()
        async def navigate_to(page, url): ...
    """
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(
            multiplier=config.RETRY_BASE_WAIT_SECONDS,
            max=config.RETRY_MAX_WAIT_SECONDS,
        ),
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


# ── AJAX / load-state helpers ──────────────────────────────────────────────────

async def wait_for_exhibitor_cards(page, timeout_ms: int = config.AJAX_WAIT_TIMEOUT_MS) -> bool:
    """
    Wait for at least one exhibitor card / link to appear in the DOM.
    Tries all selectors in config.SELECTORS["exhibitor_cards"].
    Returns True if any card appeared before timeout, False otherwise.
    """
    selectors = [s.strip() for s in config.SELECTORS["exhibitor_cards"].split(",")]
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, timeout=timeout_ms, state="attached")
            return True
        except Exception:
            pass
    logger.warning("Timed out waiting for exhibitor cards to appear.")
    return False


async def wait_for_network_idle(page, timeout_ms: int = 8_000) -> None:
    """Wait until no network connections are active for 500 ms (or timeout)."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        # networkidle can be flaky on JavaScript-heavy pages; continue anyway
        pass


async def safe_navigate(page, url: str) -> bool:
    """
    Navigate to url with the configured timeout.
    Returns True on success, False if navigation raises an exception.
    Logs the error and takes a screenshot on failure.
    """
    try:
        await page.goto(url, timeout=config.PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
        return True
    except Exception as exc:
        logger.error(f"Navigation failed for {url}: {exc}")
        await screenshot_on_error(page, url.split("/")[-1] or "navigate")
        return False
