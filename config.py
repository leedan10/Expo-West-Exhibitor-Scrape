"""
Central configuration for the Expo West 2026 Exhibitor Scraper.

Data sources (as confirmed by user):
  Listing page : https://www.expowest.com/en/exhibitor-list/2026-exhibitor-list.html
  Detail pages : https://attend.expowest.com/widget/event/natural-products-expo-west-2026/exhibitor/{id}

attend.expowest.com is a white-labelled Swapcard instance.  The widget pages
make GraphQL calls to fetch exhibitor data; the scraper intercepts these
responses for fast, reliable structured extraction.
"""
from pathlib import Path

# ── Project paths ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
LOG_DIR = BASE_DIR / "logs"
SCREENSHOT_DIR = BASE_DIR / "screenshots"
CHECKPOINT_DIR = BASE_DIR / "checkpoints"

for _dir in [DATA_DIR, OUTPUT_DIR, LOG_DIR, SCREENSHOT_DIR, CHECKPOINT_DIR]:
    _dir.mkdir(exist_ok=True)

# ── Target URLs ────────────────────────────────────────────────────────────────
# Official Expo West exhibitor listing (Phase 1 discovery)
EXHIBITOR_LIST_URL = (
    "https://www.expowest.com/en/exhibitor-list/2026-exhibitor-list.html"
)

# Swapcard-powered event platform
BASE_URL = "https://attend.expowest.com"
EVENT_SLUG = "natural-products-expo-west-2026"

# Base URL for individual exhibitor detail pages
EXHIBITOR_DETAIL_BASE = f"{BASE_URL}/widget/event/{EVENT_SLUG}/exhibitor/"

# Fallback: Swapcard event widget listing (used when expowest.com has no links)
EVENT_WIDGET_URL = f"{BASE_URL}/widget/event/{EVENT_SLUG}"

# Swapcard GraphQL endpoint (the widget calls this; we intercept the response)
GRAPHQL_URL = f"{BASE_URL}/graphql"

# ── Rate limiting ──────────────────────────────────────────────────────────────
MIN_DELAY_SECONDS = 2.0
MAX_DELAY_SECONDS = 4.0
PAGE_LOAD_TIMEOUT_MS = 45_000   # Swapcard React apps take longer than static pages
AJAX_WAIT_TIMEOUT_MS = 20_000

# ── Retry configuration ────────────────────────────────────────────────────────
MAX_RETRIES = 3
RETRY_BASE_WAIT_SECONDS = 5.0
RETRY_MAX_WAIT_SECONDS = 60.0

# ── Browser configuration ──────────────────────────────────────────────────────
HEADLESS = True
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
CHECKPOINT_INTERVAL = 50

# ── User-agent pool ────────────────────────────────────────────────────────────
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

# ── CSS selectors ──────────────────────────────────────────────────────────────
# Swapcard widget uses React with hashed class names that change per deployment.
# These selectors cover common stable patterns; GraphQL interception is preferred.
SELECTORS = {
    # Any link to an exhibitor detail page (both on expowest.com and attend domain)
    "exhibitor_cards": (
        f"a[href*='{EVENT_SLUG}/exhibitor/'], "
        "a[href*='attend.expowest.com/widget'], "
        "a[href*='/exhibitor/']"
    ),

    # Pagination
    "pagination_next": (
        "button[aria-label*='next' i], .pagination .next, "
        "a.page-next, [class*='paginator'] .next, "
        "[class*='load-more' i]"
    ),
    "pagination_pages": ".pagination a[data-page], [class*='paginator'] a",

    # Team member cards on Swapcard exhibitor pages
    "team_member_cards": (
        "[class*='PersonCard'], [class*='MemberCard'], "
        "[class*='person-card' i], [class*='member-card' i]"
    ),
}

# ── Domains to exclude from company URL extraction ─────────────────────────────
EXCLUDED_DOMAINS = {
    "smallworldlabs.com",
    "expowest.com",
    "attend.expowest.com",
    "app.swapcard.com",
    "swapcard.com",
}

# ── Social media platform → domain mapping ─────────────────────────────────────
SOCIAL_PLATFORM_DOMAINS: dict[str, list[str]] = {
    "facebook":  ["facebook.com"],
    "twitter":   ["twitter.com", "x.com"],
    "linkedin":  ["linkedin.com"],
    "instagram": ["instagram.com"],
    "youtube":   ["youtube.com", "youtu.be"],
    "tiktok":    ["tiktok.com"],
    "pinterest": ["pinterest.com"],
}

# All social domains (flat set for quick membership testing)
SOCIAL_DOMAINS: set[str] = {
    d for domains in SOCIAL_PLATFORM_DOMAINS.values() for d in domains
}

# Swapcard socialNetworks[].type → platform key
SWAPCARD_SOCIAL_TYPE_MAP: dict[str, str] = {
    "TWITTER":   "twitter",
    "X":         "twitter",
    "FACEBOOK":  "facebook",
    "LINKEDIN":  "linkedin",
    "INSTAGRAM": "instagram",
    "YOUTUBE":   "youtube",
    "TIKTOK":    "tiktok",
    "PINTEREST": "pinterest",
}
