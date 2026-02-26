"""
Checkpoint / resume system.
Saves progress to JSON files so a crashed scraper can restart where it left off.

Usage:
    links_cache = ExhibitorLinkCache()
    if not links_cache.is_populated():
        links = discover_all_links(page)
        links_cache.set(links)

    scraped = ScrapedCache()
    for link in links_cache.links:
        if scraped.has(link["slug"]):
            continue  # already done
        data = scrape_detail(link)
        scraped.mark_done(link["slug"], data)
"""
import json
import logging
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger("expowest_scraper.checkpoint")


# ── Low-level JSON helpers ─────────────────────────────────────────────────────

def load_json(path: Path, default: Any = None) -> Any:
    """Load JSON from path; return default if file missing or corrupt."""
    if default is None:
        default = {}
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Could not load checkpoint {path}: {exc}")
        return default


def save_json(path: Path, data: Any) -> None:
    """Atomically save data as JSON to path (write to .tmp then rename)."""
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(path)
    except OSError as exc:
        logger.error(f"Could not save checkpoint {path}: {exc}")


# ── Exhibitor link cache ───────────────────────────────────────────────────────

class ExhibitorLinkCache:
    """
    Stores the list of all exhibitor URLs/slugs discovered from the listing page.
    Persisted so we don't re-crawl the listing if the scraper crashes mid-detail.

    Each entry: {"url": str, "slug": str | None, "booth_id": str | None}
    """

    def __init__(self) -> None:
        self._path = config.EXHIBITOR_LINKS_CACHE
        raw = load_json(self._path, default={"links": []})
        self.links: list[dict] = raw.get("links", [])

    def set(self, links: list[dict]) -> None:
        self.links = links
        save_json(self._path, {"links": links})
        logger.info(f"Cached {len(links)} exhibitor links → {self._path}")

    def is_populated(self) -> bool:
        return len(self.links) > 0

    def clear(self) -> None:
        self.links = []
        if self._path.exists():
            self._path.unlink()
        logger.info("Exhibitor link cache cleared.")


# ── Scraped exhibitor cache ────────────────────────────────────────────────────

class ScrapedCache:
    """
    Records which exhibitors have already been successfully scraped.
    Key: exhibitor slug (or booth_id string for slug-less entries).
    Value: the scraped data dict.
    """

    def __init__(self) -> None:
        self._path = config.SCRAPED_CACHE
        self._data: dict[str, dict] = load_json(self._path, default={})

    def has(self, key: str) -> bool:
        return key in self._data

    def mark_done(self, key: str, record: dict) -> None:
        self._data[key] = record
        save_json(self._path, self._data)

    def all_records(self) -> list[dict]:
        return list(self._data.values())

    def count(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        self._data = {}
        if self._path.exists():
            self._path.unlink()
        logger.info("Scraped cache cleared.")


# ── Team members cache ─────────────────────────────────────────────────────────

class TeamMembersCache:
    """
    Accumulates team member records across all exhibitors.
    Saved incrementally alongside ScrapedCache.
    """

    def __init__(self) -> None:
        self._path = config.CHECKPOINT_DIR / "team_members.json"
        self._data: list[dict] = load_json(self._path, default=[])

    def extend(self, records: list[dict]) -> None:
        self._data.extend(records)
        save_json(self._path, self._data)

    def all_records(self) -> list[dict]:
        return list(self._data)

    def count(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        self._data = []
        if self._path.exists():
            self._path.unlink()


# ── Progress tracker ───────────────────────────────────────────────────────────

class ProgressTracker:
    """
    High-level progress: stores current page, total pages, total exhibitors, phase.

    Usage:
        progress = ProgressTracker()
        progress.set("phase", "listing")
        progress.set("current_page", 3)
        current = progress.get("current_page", default=1)
    """

    def __init__(self) -> None:
        self._path = config.PROGRESS_FILE
        self._data: dict = load_json(self._path, default={})

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value
        save_json(self._path, self._data)

    def update(self, **kwargs) -> None:
        self._data.update(kwargs)
        save_json(self._path, self._data)

    def clear(self) -> None:
        self._data = {}
        if self._path.exists():
            self._path.unlink()
