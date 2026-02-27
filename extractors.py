"""
HTML parsing and data extraction for the Expo West 2026 scraper.

Data flows in priority order for each exhibitor detail page:
  1. GraphQL JSON  — intercepted when Swapcard's React widget fetches its data.
                     Most complete and reliable; used whenever available.
  2. Embedded JSON — some React apps hydrate via window.__NEXT_DATA__ or
                     similar <script> tags; parsed as a fallback.
  3. HTML          — BeautifulSoup parsing of the rendered widget page.
                     Used when neither JSON source is available.

For the exhibitor LISTING page (expowest.com) the scraper only needs URLs,
so we parse <a href> elements that match the attend.expowest.com/exhibitor pattern.
"""
import json
import logging
import re
from urllib.parse import urlparse, parse_qs, unquote

from bs4 import BeautifulSoup

import config

logger = logging.getLogger("expowest_scraper.extractors")


# ── Helpers ────────────────────────────────────────────────────────────────────

_PLATFORM_DOMAINS = config.SOCIAL_PLATFORM_DOMAINS
_SWAPCARD_TYPE_MAP = config.SWAPCARD_SOCIAL_TYPE_MAP


def _classify_social_url(href: str) -> str | None:
    """Return platform key ('facebook', 'twitter', …) or None."""
    try:
        domain = urlparse(href).netloc.lower().lstrip("www.")
    except Exception:
        return None
    for platform, domains in _PLATFORM_DOMAINS.items():
        if any(d in domain for d in domains):
            return platform
    return None


def _empty_record(source_url: str) -> dict:
    return {
        "exhibitor_name":     "",
        "booth_number":       "",
        "hall":               "",
        "information":        "",
        "product_categories": "",
        "country":            "",
        "state":              "",
        "company_url":        "",
        "facebook_url":       "",
        "twitter_url":        "",
        "linkedin_url":       "",
        "instagram_url":      "",
        "youtube_url":        "",
        "tiktok_url":         "",
        "pinterest_url":      "",
        "source_url":         source_url,
    }


# ── Exhibitor list page ────────────────────────────────────────────────────────

def extract_exhibitor_links(html: str, base_url: str = "") -> list[dict]:
    """
    Parse the expowest.com listing page (or any page) and return dicts for
    every link that points to an attend.expowest.com exhibitor detail page.

    Dict format: {"url": str, "slug": str, "booth_id": None}
    where slug is the base64 Swapcard exhibitor ID.
    """
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    results: list[dict] = []

    # Pattern: …/widget/event/{event-slug}/exhibitor/{base64-id}
    exhibitor_pattern = re.compile(
        r"/widget/event/[^/]+/exhibitor/([A-Za-z0-9+/=_-]+)"
    )

    for anchor in soup.find_all("a", href=True):
        href: str = anchor["href"]

        # Normalise relative URLs
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/") and base_url:
            href = base_url.rstrip("/") + href

        m = exhibitor_pattern.search(href)
        if not m:
            continue

        slug = m.group(1)
        # Build canonical attend.expowest.com URL
        full_url = config.EXHIBITOR_DETAIL_BASE + slug
        if full_url not in seen:
            seen.add(full_url)
            results.append({"url": full_url, "slug": slug, "booth_id": None})

    logger.debug(f"Extracted {len(results)} exhibitor links from HTML")
    return results


def extract_total_pages(html: str) -> int:
    """
    Try to detect the total number of listing pages from DOM pagination.
    Returns 1 if pagination is not detected.
    """
    soup = BeautifulSoup(html, "lxml")
    max_page = 1

    for el in soup.find_all(attrs={"data-page": True}):
        try:
            max_page = max(max_page, int(el["data-page"]))
        except (ValueError, KeyError):
            pass

    for el in soup.select(
        ".pagination a, .page-numbers, [class*='paginator'] a, "
        "[class*='pagination'] a, .pager a"
    ):
        text = el.get_text(strip=True)
        if text.isdigit():
            max_page = max(max_page, int(text))

    page_text = soup.get_text(" ")
    m = re.search(r"page\s+\d+\s+of\s+(\d+)", page_text, re.I)
    if m:
        max_page = max(max_page, int(m.group(1)))

    logger.debug(f"Detected {max_page} total pages")
    return max_page


# ── GraphQL / API JSON parsing (primary extraction path) ──────────────────────

def parse_graphql_exhibitor(json_body: dict, source_url: str) -> dict | None:
    """
    Parse a Swapcard GraphQL response body and return an exhibitor record dict,
    or None if no recognisable exhibitor data is present.

    Handles multiple query shapes:
      • { data: { exhibitor: { … } } }
      • { data: { planning: { exhibitor: { … } } } }
      • { data: { event: { exhibitors: { edges/nodes: [ { node/…: { … } } ] } } } }
      • { data: { plannings: { nodes: [ { exhibitor: { … } } ] } } }
    """
    if not isinstance(json_body, dict):
        return None

    data = json_body.get("data") or json_body
    if not isinstance(data, dict):
        return None

    # Try to find a single exhibitor object
    exhibitor_obj = _dig_single_exhibitor(data)
    if exhibitor_obj:
        return _exhibitor_obj_to_record(exhibitor_obj, source_url)

    return None


def parse_graphql_exhibitor_list(json_body: dict) -> list[dict]:
    """
    Parse a Swapcard GraphQL listing response and return a list of
    {"url", "slug", "booth_id"} dicts for all exhibitors found.
    """
    if not isinstance(json_body, dict):
        return []
    data = json_body.get("data") or json_body
    if not isinstance(data, dict):
        return []

    nodes = _dig_exhibitor_list_nodes(data)
    results: list[dict] = []
    seen: set[str] = set()

    for node in nodes:
        eid = node.get("id", "")
        if not eid:
            continue
        # Swapcard IDs look like "Exhibitor_2357707" — encode to base64 URL segment
        import base64
        slug = base64.b64encode(eid.encode()).decode().rstrip("=") + "="
        # Or the ID might already be the URL slug directly
        # Try both forms; the detail page URL might use the raw ID
        url = config.EXHIBITOR_DETAIL_BASE + slug
        if url not in seen:
            seen.add(url)
            results.append({"url": url, "slug": slug, "booth_id": None, "_raw_id": eid})

    logger.debug(f"Parsed {len(results)} exhibitor links from GraphQL listing response")
    return results


def _dig_single_exhibitor(data: dict) -> dict | None:
    """Recursively find a single exhibitor object in a GraphQL data dict."""
    # Direct: data.exhibitor
    if "exhibitor" in data and isinstance(data["exhibitor"], dict):
        return data["exhibitor"]

    # Nested: data.planning.exhibitor or data.event.exhibitor
    for key in ("planning", "event", "session", "booth"):
        if key in data and isinstance(data[key], dict):
            result = _dig_single_exhibitor(data[key])
            if result:
                return result

    # data.exhibitors.edges[0].node  (when only one exhibitor is in the list)
    for list_key in ("exhibitors", "plannings"):
        container = data.get(list_key)
        if isinstance(container, dict):
            edges = container.get("edges") or container.get("nodes") or []
            if len(edges) == 1:
                node = edges[0]
                if isinstance(node, dict):
                    return node.get("node") or node.get("exhibitor") or node

    return None


def _dig_exhibitor_list_nodes(data: dict) -> list[dict]:
    """Return all exhibitor-like objects from a GraphQL listing response."""
    nodes: list[dict] = []

    def _walk(obj):
        if not isinstance(obj, dict):
            return
        # Found an exhibitor node?
        if "name" in obj and ("boothNumber" in obj or "id" in obj):
            if obj.get("id", "").startswith("Exhibitor"):
                nodes.append(obj)
                return

        for v in obj.values():
            if isinstance(v, dict):
                _walk(v)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        # Check if it's an edge wrapper
                        inner = item.get("node") or item.get("exhibitor") or item
                        _walk(inner)

    _walk(data)
    return nodes


def _exhibitor_obj_to_record(obj: dict, source_url: str) -> dict:
    """Convert a Swapcard exhibitor JSON object to our flat record dict."""
    result = _empty_record(source_url)

    result["exhibitor_name"] = obj.get("name") or obj.get("displayName") or ""
    result["information"] = obj.get("description") or obj.get("bio") or ""

    # Booth & Hall — may be separate fields or a combined string
    result["booth_number"] = (
        obj.get("boothNumber") or obj.get("booth") or obj.get("booth_number") or ""
    )
    result["hall"] = (
        obj.get("hall") or obj.get("hallName") or obj.get("building") or ""
    )

    # If booth contains hall info (e.g. "North Hall, N519"), split it out
    if result["booth_number"] and not result["hall"]:
        combined = result["booth_number"]
        hall_m = re.search(
            r'((?:North|South|East|West|Central|Main)\s+Hall[^,]*)',
            combined, re.I
        )
        if hall_m:
            result["hall"] = hall_m.group(1).strip()
            result["booth_number"] = combined.replace(hall_m.group(0), "").strip(", ")

    # Location
    result["country"] = obj.get("country") or obj.get("countryCode") or ""
    result["state"] = obj.get("state") or obj.get("stateCode") or obj.get("region") or ""

    # Company URL — may be in websiteUrl or inside contacts[]
    result["company_url"] = (
        obj.get("websiteUrl") or obj.get("website") or obj.get("web") or ""
    )

    # Social networks (Swapcard standard: socialNetworks[{type, url}])
    for sn in obj.get("socialNetworks") or obj.get("social_networks") or []:
        sn_type = str(sn.get("type") or "").upper()
        sn_url = sn.get("url") or sn.get("link") or ""
        if not sn_url:
            continue
        platform = _SWAPCARD_TYPE_MAP.get(sn_type)
        if platform:
            field = f"{platform}_url"
            if not result[field]:
                result[field] = sn_url
        elif sn_type in ("WEBSITE", "WEB", "OTHER") and not result["company_url"]:
            result["company_url"] = sn_url

    # contacts[] — alternative shape some Swapcard instances use
    for contact in obj.get("contacts") or []:
        c_type = str(contact.get("type") or "").upper()
        c_val = contact.get("value") or contact.get("url") or ""
        if not c_val:
            continue
        platform = _SWAPCARD_TYPE_MAP.get(c_type)
        if platform:
            field = f"{platform}_url"
            if not result[field]:
                result[field] = c_val
        elif c_type in ("WEBSITE", "URL", "WEB") and not result["company_url"]:
            result["company_url"] = c_val

    # Product categories — categories[{label, parentLabel}]
    cat_parts: list[str] = []
    for cat in obj.get("categories") or obj.get("tags") or []:
        parent = cat.get("parentLabel") or cat.get("parent") or ""
        label = cat.get("label") or cat.get("name") or ""
        if parent and label:
            cat_parts.append(f"{parent} > {label}")
        elif label:
            cat_parts.append(label)
    result["product_categories"] = ", ".join(cat_parts)

    return result


def parse_graphql_team_members(json_body: dict, exhibitor_name: str) -> list[dict]:
    """
    Extract team member records from a Swapcard GraphQL response.
    Returns [{exhibitor_name, team_member_name, job_title}].
    """
    if not isinstance(json_body, dict):
        return []

    data = json_body.get("data") or json_body
    exhibitor_obj = _dig_single_exhibitor(data) if isinstance(data, dict) else None

    people = []
    if exhibitor_obj:
        people = (
            exhibitor_obj.get("people") or
            exhibitor_obj.get("speakers") or
            exhibitor_obj.get("contacts") or
            []
        )

    results: list[dict] = []
    for person in people:
        if not isinstance(person, dict):
            continue
        first = person.get("firstName") or person.get("first_name") or ""
        last = person.get("lastName") or person.get("last_name") or ""
        name = f"{first} {last}".strip() or person.get("name") or person.get("displayName") or ""
        title = (
            person.get("jobTitle") or person.get("job_title") or
            person.get("role") or person.get("title") or ""
        )
        if name:
            results.append({
                "exhibitor_name": exhibitor_name,
                "team_member_name": name,
                "job_title": title,
            })

    return results


# ── Embedded JSON in <script> tags ────────────────────────────────────────────

def extract_embedded_json(html: str) -> list[dict]:
    """
    Try to extract JSON objects embedded in <script> tags by React apps
    (e.g. window.__NEXT_DATA__, __APOLLO_STATE__, __INITIAL_STATE__).
    Returns a list of decoded JSON objects.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[dict] = []
    patterns = [
        re.compile(r'window\.__(?:NEXT_DATA|INITIAL_STATE|APOLLO_STATE|DATA)__\s*=\s*(\{.+?\});?\s*$', re.S),
        re.compile(r'<script[^>]+type="application/json"[^>]*>(.+?)</script>', re.S | re.I),
    ]

    for script in soup.find_all("script"):
        text = script.string or ""
        if not text.strip():
            continue
        for pat in patterns:
            m = pat.search(text)
            if m:
                try:
                    obj = json.loads(m.group(1))
                    results.append(obj)
                except json.JSONDecodeError:
                    pass

        # Also try raw JSON script blocks
        text = text.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                obj = json.loads(text)
                results.append(obj if isinstance(obj, dict) else {"items": obj})
            except json.JSONDecodeError:
                pass

    return results


# ── HTML extraction (Swapcard widget fallback) ─────────────────────────────────

def extract_exhibitor_detail(html: str, source_url: str) -> dict:
    """
    Parse a Swapcard exhibitor widget page from HTML.
    Used when GraphQL interception did not capture the response.
    """
    soup = BeautifulSoup(html, "lxml")
    result = _empty_record(source_url)

    try:
        _html_parse_name(soup, result)
        _html_parse_booth_and_hall(soup, result)
        _html_parse_description(soup, result)
        _html_parse_categories(soup, result)
        _html_parse_location(soup, result)
        _html_parse_links(soup, result)
    except Exception as exc:
        logger.error(f"HTML parse error for {source_url}: {exc}", exc_info=True)

    return result


def _html_parse_name(soup: BeautifulSoup, result: dict) -> None:
    """Company name — Swapcard puts it in an <h1> on the widget page."""
    # Try h1 first
    h1 = soup.select_one("h1")
    if h1:
        text = h1.get_text(strip=True)
        if text:
            result["exhibitor_name"] = text
            return

    # Fallback: prominent heading or aria-label
    for sel in ["h2", "[class*='name' i]", "[class*='title' i]", "[aria-label]"]:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text and len(text) < 200:
                result["exhibitor_name"] = text
                return


def _html_parse_booth_and_hall(soup: BeautifulSoup, result: dict) -> None:
    """
    Booth and Hall — Swapcard typically shows them together near the top.
    Common patterns:
      "North Hall Level 100 · Booth N519"
      "Booth: N519  |  Hall: North Hall"
    """
    full_text = soup.get_text(separator=" ")[:5000]

    # Combined pattern: "Hall X, Booth Y" or "North Hall Level 100 · N519"
    hall_pat = re.compile(
        r'((?:North|South|East|West|Central|Main|Upper|Lower|Lobby|Level)'
        r'\s+(?:Hall|Pavilion|Building)[^,·|\n]*)',
        re.I,
    )
    booth_pat = re.compile(r'[Bb]ooth\s*[#:]?\s*([A-Z]?\d+[A-Z]?)', re.I)
    booth_code_pat = re.compile(r'\b([NSEWC]\d{3,4})\b')

    # Hall
    hm = hall_pat.search(full_text)
    if hm:
        result["hall"] = hm.group(1).strip().rstrip("·,|; ")

    # Booth number
    bm = booth_pat.search(full_text)
    if bm:
        result["booth_number"] = bm.group(1)
    else:
        # Try raw booth code like "N519"
        bcm = booth_code_pat.search(full_text[: 2000])
        if bcm:
            result["booth_number"] = bcm.group(1)


def _html_parse_description(soup: BeautifulSoup, result: dict) -> None:
    """Company description — look for substantial paragraph/div text blocks."""
    # Try common Swapcard description containers
    for sel in [
        "[class*='description' i]",
        "[class*='about' i]",
        "[class*='bio' i]",
        "[class*='summary' i]",
        "main p",
        "article p",
    ]:
        els = soup.select(sel)
        if not els:
            continue
        texts = [e.get_text(separator=" ", strip=True) for e in els]
        long_texts = [t for t in texts if len(t) > 80]
        if long_texts:
            result["information"] = " ".join(long_texts)
            return

    # Last resort: find the longest <p> block on the page
    paras = soup.find_all("p")
    if paras:
        best = max(paras, key=lambda p: len(p.get_text(strip=True)), default=None)
        if best:
            text = best.get_text(separator=" ", strip=True)
            if len(text) > 80:
                result["information"] = text


def _html_parse_categories(soup: BeautifulSoup, result: dict) -> None:
    """Product categories — look for breadcrumb-style "Food > Snacks > Jerky" text."""
    full_text = soup.get_text(separator="\n")

    # Look for lines with " > " separator
    for line in full_text.split("\n"):
        line = line.strip()
        if " > " in line and len(line) < 300:
            result["product_categories"] = line
            return

    # Look for elements labelled "category" / "categories"
    for sel in ["[class*='categor' i]", "[class*='tag' i]", "[class*='sector' i]"]:
        els = soup.select(sel)
        if els:
            texts = [e.get_text(strip=True) for e in els if e.get_text(strip=True)]
            if texts:
                result["product_categories"] = ", ".join(texts[:10])
                return


def _html_parse_location(soup: BeautifulSoup, result: dict) -> None:
    """Country and state — look for location-related elements."""
    full_text = soup.get_text(separator="\n")

    # Common US states abbreviation check
    us_states = {
        "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL",
        "IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT",
        "NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI",
        "SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC",
    }

    for sel in [
        "[class*='country' i]",
        "[class*='location' i]",
        "[class*='address' i]",
        "[class*='city' i]",
    ]:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text:
                if not result["country"]:
                    result["country"] = text
                break

    # Look for "United States" or country-like text
    if not result["country"]:
        m = re.search(r'\b(United States|Canada|Australia|United Kingdom|[A-Z][a-z]+ [A-Z][a-z]+)\b', full_text)
        if m:
            result["country"] = m.group(1)

    # State abbreviation
    if not result["state"]:
        # Pattern like "Chicago, IL" or "IL, 60601"
        state_m = re.search(r'\b([A-Z]{2})[,\s]+\d{5}\b', full_text)
        if state_m and state_m.group(1) in us_states:
            result["state"] = state_m.group(1)


def _html_parse_links(soup: BeautifulSoup, result: dict) -> None:
    """Company website URL + social media links from all <a href> elements."""
    for anchor in soup.find_all("a", href=True):
        href: str = anchor["href"]
        if not href.startswith("http"):
            continue

        platform = _classify_social_url(href)
        if platform:
            field = f"{platform}_url"
            if not result[field]:
                result[field] = href
            continue

        parsed = urlparse(href)
        domain = parsed.netloc.lower().lstrip("www.")
        if any(excl in domain for excl in config.EXCLUDED_DOMAINS):
            continue

        if not result["company_url"]:
            anchor_text = anchor.get_text(strip=True).lower()
            parent_text = anchor.parent.get_text(strip=True).lower() if anchor.parent else ""
            context = anchor_text + " " + parent_text
            # Prefer explicitly labelled website links
            if any(kw in context for kw in ("website", "web site", "homepage", "visit us", "www")):
                result["company_url"] = href
            else:
                result["company_url"] = href   # take first external as default


# ── Team member extraction (HTML fallback) ────────────────────────────────────

def extract_team_members(html: str, exhibitor_name: str) -> list[dict]:
    """
    Extract team members from a Swapcard widget page HTML.
    GraphQL extraction (parse_graphql_team_members) is preferred.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[dict] = []

    card_selectors = [
        # Swapcard-specific class patterns
        "[class*='PersonCard']",
        "[class*='MemberCard']",
        "[class*='SpeakerCard']",
        "[class*='person-card' i]",
        "[class*='member-card' i]",
        "[class*='contact-card' i]",
        # Team tab content
        "[id$='_team'] li",
        "[id$='_people'] li",
        "[id*='team' i] [class*='card' i]",
        "[id*='people' i] [class*='card' i]",
        # SmallWorld Labs patterns (kept as fallback)
        ".swl-member", ".org-member", ".member-card", ".staff-item",
        "[class*='member-list'] li", "[class*='team-members'] li",
    ]

    name_selectors = [
        "h4", "h5", "h3", "strong",
        "[class*='name' i]", "[class*='fullName' i]",
        "[class*='firstName' i]", "[class*='displayName' i]",
    ]
    title_selectors = [
        "[class*='jobTitle' i]", "[class*='job-title' i]",
        "[class*='role' i]", "[class*='position' i]",
        "[class*='title' i]", "em", "small", "span",
    ]

    for selector in card_selectors:
        try:
            cards = soup.select(selector)
        except Exception:
            continue
        if not cards:
            continue

        for card in cards:
            name = _first_text(card, name_selectors)
            title = _first_text(card, title_selectors)
            if name and name != exhibitor_name:
                results.append({
                    "exhibitor_name": exhibitor_name,
                    "team_member_name": name,
                    "job_title": title,
                })

        if results:
            break

    if not results:
        logger.debug(f"No public team members found (HTML) for '{exhibitor_name}'")
    return results


def _first_text(container, selectors: list[str]) -> str:
    for sel in selectors:
        el = container.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text:
                return text
    return ""
