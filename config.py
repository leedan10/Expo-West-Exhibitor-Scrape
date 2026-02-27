"""
Central configuration for the Expo West 2026 Exhibitor Scraper.
All tunable parameters live here — no magic constants in scraper code.
To debug locally, set HEADLESS = False.
"""
from pathlib import Path

# ── Project paths ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
LOG_DIR = BASE_DIR / "logs"
SCREENSHOT_DIR = BASE_DIR / "screenshots"
CHECKPOINT_DIR = BASE_DIR / "checkpoints"

# Ensure directories exist at import time
for _dir in [DATA_DIR, OUTPUT_DIR, LOG_DIR, SCREENSHOT_DIR, CHECKPOINT_DIR]:
    _dir.mkdir(exist_ok=True)

# ── Target URLs ────────────────────────────────────────────────────────────────
BASE_URL = "https://expowest26.smallworldlabs.com"
EXHIBITOR_LIST_URL = f"{BASE_URL}/exhibitors"
EXHIBITOR_DETAIL_URL_TEMPLATE = f"{BASE_URL}/co/{{slug}}"

# Fallback: ID-based URL (used when slug is unavailable)
# page_id=2424 is the SmallWorld Labs exhibitor profile page for this event
EXHIBITOR_ID_URL_TEMPLATE = (
    f"{BASE_URL}/?page_id=2424&boothId=boothId%3D{{booth_id}}"
)

# ── Rate limiting ──────────────────────────────────────────────────────────────
MIN_DELAY_SECONDS = 2.0       # minimum wait between requests
MAX_DELAY_SECONDS = 4.0       # maximum wait (random within range)
PAGE_LOAD_TIMEOUT_MS = 30_000  # 30 seconds for full page load
AJAX_WAIT_TIMEOUT_MS = 15_000  # 15 seconds to wait for AJAX content

# ── Retry configuration ────────────────────────────────────────────────────────
MAX_RETRIES = 3
RETRY_BASE_WAIT_SECONDS = 5.0   # base for exponential backoff
RETRY_MAX_WAIT_SECONDS = 60.0

# ── Browser configuration ──────────────────────────────────────────────────────
HEADLESS = True   # set False for local debugging (watch browser in real time)
BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
]

# ── Output files ───────────────────────────────────────────────────────────────
EXHIBITORS_OUTPUT = OUTPUT_DIR / "exhibitors.xlsx"
TEAM_MEMBERS_OUTPUT = OUTPUT_DIR / "team_members.xlsx"

# ── Checkpoint files ───────────────────────────────────────────────────────────
EXHIBITOR_LINKS_CACHE = CHECKPOINT_DIR / "exhibitor_links.json"
SCRAPED_CACHE = CHECKPOINT_DIR / "scraped_exhibitors.json"
PROGRESS_FILE = CHECKPOINT_DIR / "progress.json"

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_FILE = LOG_DIR / "scraper.log"
LOG_LEVEL = "DEBUG"

# ── Checkpoint interval ────────────────────────────────────────────────────────
# Write partial Excel output every N exhibitors (protects against crashes)
CHECKPOINT_INTERVAL = 50

# ── User-agent fallback pool ───────────────────────────────────────────────────
USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
        "Gecko/20100101 Firefox/124.0"
    ),
]

# ── CSS selectors (confirmed from live page analysis) ──────────────────────────
# Update these in config.py if the site changes its HTML structure.
SELECTORS = {
    # Exhibitor list page — cards rendered via AJAX
    "exhibitor_cards": "a[href*='/co/']",

    # Pagination
    "pagination_next": ".pagination .next, a.page-next, [class*='paginator'] .next",
    "pagination_pages": ".pagination a[data-page], [class*='paginator'] a",

    # Exhibitor detail page
    "company_name": "#organizations_profile_0_0 h4",
    "booth_link": "a[href*='MapItBooth=']",
    "about_tab": "[id$='_about']",
    "contact_tab": "[id$='_contact']",

    # Team members (best-effort — likely login-gated)
    "team_member_cards": (
        ".member-card, .staff-item, .people-card, "
        "[class*='member-list'] li, [class*='team'] li"
    ),
    "member_name": (
        ".member-name, .staff-name, h5.name, "
        "[class*='name']:first-child"
    ),
    "member_title": (
        ".member-title, .job-title, .staff-role, "
        "[class*='title'], [class*='role']"
    ),
}

# ── Domains to exclude from company URL extraction ─────────────────────────────
EXCLUDED_DOMAINS = {
    "smallworldlabs.com",
    "expowest.com",
    "expowest26.smallworldlabs.com",
    "exhibitor.expowest.com",
    "attend.expowest.com",
}

# ── Social media domains to detect ────────────────────────────────────────────
SOCIAL_DOMAINS = {
    "facebook.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "pinterest.com",
}

# Per-platform domain mapping (used by extractors to assign the right column)
SOCIAL_PLATFORM_DOMAINS: dict[str, list[str]] = {
    "facebook":  ["facebook.com"],
    "twitter":   ["twitter.com", "x.com"],
    "linkedin":  ["linkedin.com"],
    "instagram": ["instagram.com"],
    "youtube":   ["youtube.com", "youtu.be"],
    "tiktok":    ["tiktok.com"],
    "pinterest": ["pinterest.com"],
}
