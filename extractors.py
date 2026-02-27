"""
HTML parsing and data extraction functions.
All functions accept rendered HTML strings and return clean Python dicts/lists.
No browser dependencies here — pure BeautifulSoup.

SmallWorld Labs exhibitor page structure (expowest26.smallworldlabs.com):
  - Cards link to /co/{slug}
  - Company name: #organizations_profile_0_0 h4  (fallback: first h1/h2)
  - Booth:        <a href="...MapItBooth=3312...">
  - Hall:         "Hall X" text near booth info or in profile header
  - About tab:    element whose id ends with '_about'   → description + categories
  - Contact tab:  element whose id ends with '_contact' → address, website, social
  - Social links: scanned from both tabs + whole page fallback (per-platform)
  - Team members: tab with id ending '_team' / '_people' / '_staff' (may be login-gated)
"""
import logging
import re
from urllib.parse import urlparse, parse_qs, unquote

from bs4 import BeautifulSoup

import config

logger = logging.getLogger("expowest_scraper.extractors")

# ── Social platform domain mappings ───────────────────────────────────────────

_PLATFORM_DOMAINS: dict[str, list[str]] = {
    "facebook":  ["facebook.com"],
    "twitter":   ["twitter.com", "x.com"],
    "linkedin":  ["linkedin.com"],
    "instagram": ["instagram.com"],
    "youtube":   ["youtube.com", "youtu.be"],
    "tiktok":    ["tiktok.com"],
    "pinterest": ["pinterest.com"],
}

_ALL_SOCIAL_DOMAINS: set[str] = {
    d for domains in _PLATFORM_DOMAINS.values() for d in domains
}


# ── Exhibitor list page ────────────────────────────────────────────────────────

def extract_exhibitor_links(html: str, base_url: str = config.BASE_URL) -> list[dict]:
    """
    Parse the rendered exhibitor directory HTML and return a deduplicated list
    of exhibitor link dicts: [{url, slug, booth_id}].

    Two URL patterns on SmallWorld Labs:
      1. /co/{slug}                                  ← preferred
      2. /?page_id=2424&boothId=boothId%3D{id}      ← fallback
    """
    soup = BeautifulSoup(html, "lxml")
    seen_urls: set[str] = set()
    results: list[dict] = []

    for anchor in soup.find_all("a", href=True):
        href: str = anchor["href"]

        # Normalize to absolute URL
        if href.startswith("/"):
            href = base_url.rstrip("/") + href
        elif not href.startswith("http"):
            continue

        # Pattern 1: /co/{slug}
        match = re.search(r"/co/([^/?#]+)", href)
        if match:
            slug = match.group(1)
            full_url = f"{base_url.rstrip('/')}/co/{slug}"
            if full_url not in seen_urls:
                seen_urls.add(full_url)
                results.append({"url": full_url, "slug": slug, "booth_id": None})
            continue

        # Pattern 2: page_id=2424&boothId=boothId%3D{id}
        if "page_id=2424" in href and "boothId=" in href:
            parsed = urlparse(href)
            qs = parse_qs(parsed.query)
            booth_id_raw = qs.get("boothId", [""])[0]
            booth_id = re.sub(r"[^0-9]", "", unquote(booth_id_raw))
            if href not in seen_urls and booth_id:
                seen_urls.add(href)
                results.append({"url": href, "slug": None, "booth_id": booth_id})

    logger.debug(f"Extracted {len(results)} unique exhibitor links from HTML")
    return results


def extract_total_pages(html: str) -> int:
    """
    Parse pagination controls to determine the total page count.
    SmallWorld Labs uses jsPaginator with numbered page links.
    Returns 1 if pagination is not found.
    """
    soup = BeautifulSoup(html, "lxml")
    max_page = 1

    # Strategy 1: data-page attributes
    for el in soup.find_all(attrs={"data-page": True}):
        try:
            max_page = max(max_page, int(el["data-page"]))
        except (ValueError, KeyError):
            pass

    # Strategy 2: numeric text in pagination anchors
    for el in soup.select(
        ".pagination a, .page-numbers, [class*='paginator'] a, "
        "[class*='pagination'] a, .pager a"
    ):
        text = el.get_text(strip=True)
        if text.isdigit():
            max_page = max(max_page, int(text))

    # Strategy 3: "Page X of Y" or "Showing X-Y of Z" style text
    page_text = soup.get_text(" ")
    m = re.search(r"page\s+\d+\s+of\s+(\d+)", page_text, re.I)
    if m:
        max_page = max(max_page, int(m.group(1)))

    logger.debug(f"Detected {max_page} total pages")
    return max_page


# ── Internal tab-finder helper ─────────────────────────────────────────────────

def _find_tab(soup: BeautifulSoup, selectors: list[str]):
    """Try each CSS selector in order; return the first matching element or None."""
    for sel in selectors:
        try:
            el = soup.select_one(sel)
            if el:
                return el
        except Exception:
            pass
    return None


# ── Social media classification ────────────────────────────────────────────────

def _classify_social_url(href: str) -> str | None:
    """Return the platform key ('facebook', 'twitter', …) or None."""
    try:
        domain = urlparse(href).netloc.lower().lstrip("www.")
    except Exception:
        return None
    for platform, domains in _PLATFORM_DOMAINS.items():
        if any(d in domain for d in domains):
            return platform
    return None


def _is_social_url(href: str) -> bool:
    return _classify_social_url(href) is not None


# ── Exhibitor detail page ──────────────────────────────────────────────────────

def extract_exhibitor_detail(html: str, source_url: str) -> dict:
    """
    Parse an individual exhibitor detail page and return a flat dict
    matching the exhibitors.xlsx column schema.

    Returns an empty-valued dict (never raises) so the caller can always
    store a record even when parsing partially fails.
    """
    soup = BeautifulSoup(html, "lxml")

    result = {
        "exhibitor_name":   "",
        "booth_number":     "",
        "hall":             "",
        "information":      "",
        "product_categories": "",
        "country":          "",
        "company_url":      "",
        # Per-platform social media columns
        "facebook_url":     "",
        "twitter_url":      "",
        "linkedin_url":     "",
        "instagram_url":    "",
        "youtube_url":      "",
        "tiktok_url":       "",
        "pinterest_url":    "",
        "source_url":       source_url,
    }

    try:
        _parse_name(soup, result)
        _parse_booth(soup, result)
        _parse_hall(soup, result)
        _parse_about_tab(soup, result)
        _parse_contact_tab(soup, result)
    except Exception as exc:
        logger.error(f"Unexpected parse error for {source_url}: {exc}", exc_info=True)

    return result


def _parse_name(soup: BeautifulSoup, result: dict) -> None:
    # Primary: #organizations_profile_0_0 h4
    wrapper = soup.select_one("#organizations_profile_0_0")
    if wrapper:
        h4 = wrapper.select_one("h4")
        if h4:
            result["exhibitor_name"] = h4.get_text(strip=True)
            return

    # Fallback 1: any element with class containing 'org-name', 'company-name', etc.
    for sel in [
        "[class*='org-name']", "[class*='company-name']",
        "[class*='organization-name']", "[class*='profile-name']",
        "[class*='exhibitor-name']",
    ]:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text:
                result["exhibitor_name"] = text
                return

    # Fallback 2: first <h1> or <h2> on page
    for tag in ("h1", "h2"):
        el = soup.select_one(tag)
        if el:
            text = el.get_text(strip=True)
            if text:
                result["exhibitor_name"] = text
                return


def _parse_booth(soup: BeautifulSoup, result: dict) -> None:
    booths: list[str] = []
    for anchor in soup.find_all("a", href=re.compile(r"MapItBooth=")):
        text = anchor.get_text(strip=True)
        # Text pattern: "Booth #3312" or "3312"
        m = re.search(r"#?\s*(\d+)", text)
        if m and m.group(1) not in booths:
            booths.append(m.group(1))
        # Fallback: read from URL parameter
        url_m = re.search(r"MapItBooth=(\d+)", anchor.get("href", ""))
        if url_m and url_m.group(1) not in booths:
            booths.append(url_m.group(1))
    result["booth_number"] = ", ".join(booths)


def _parse_hall(soup: BeautifulSoup, result: dict) -> None:
    """
    Try to extract hall / pavilion / building info.
    Checks booth anchor text/siblings, profile header, and then the wider page.
    """
    hall_pattern = re.compile(
        r'\b(Hall|Pavilion|Building|Level|Section)\s*[:\-]?\s*([A-Z0-9]+)',
        re.IGNORECASE,
    )

    # 1. Check booth-related anchors and their parent context
    for anchor in soup.find_all("a", href=re.compile(r"MapItBooth=")):
        # Check link text
        text = anchor.get_text(strip=True)
        m = hall_pattern.search(text)
        if m:
            result["hall"] = m.group(0).strip()
            return
        # Check the surrounding container (parent, grandparent)
        for el in [anchor.parent, anchor.parent.parent if anchor.parent else None]:
            if el:
                container_text = el.get_text(separator=" ", strip=True)
                m = hall_pattern.search(container_text)
                if m:
                    result["hall"] = m.group(0).strip()
                    return

    # 2. Elements whose class/id contains 'booth', 'hall', 'location', 'floor'
    for el in soup.find_all(
        True,
        attrs={"class": re.compile(r"booth|hall|location|floor", re.I)},
    ):
        text = el.get_text(separator=" ", strip=True)
        m = hall_pattern.search(text)
        if m:
            result["hall"] = m.group(0).strip()
            return

    for el in soup.find_all(
        True,
        attrs={"id": re.compile(r"booth|hall|location|floor", re.I)},
    ):
        text = el.get_text(separator=" ", strip=True)
        m = hall_pattern.search(text)
        if m:
            result["hall"] = m.group(0).strip()
            return

    # 3. Scan first 4 000 chars of page text (profile header area)
    page_text = soup.get_text(separator=" ")[:4000]
    m = hall_pattern.search(page_text)
    if m:
        result["hall"] = m.group(0).strip()


def _parse_about_tab(soup: BeautifulSoup, result: dict) -> None:
    """
    Extract description text and product categories from the About tab.

    Tries <p> tags first; if those are empty falls back to any block-level
    element that contains meaningful text (handles sites that use <div> markup).
    Also uses a heading-based search for explicit 'Product Categories' labels.
    """
    about = _find_tab(soup, [
        "[id$='_about']",
        "[id*='about'][class*='tab']",
        "[id*='about'][class*='pane']",
        "#tab-about",
        "#about",
        "[data-tab='about']",
        "[aria-label*='about' i]",
    ])
    if not about:
        return

    # ── Collect candidate text blocks ─────────────────────────────────────────
    def _is_leaf_block(el) -> bool:
        """True if element has no block-level children (avoids double-counting)."""
        block_tags = {"p", "div", "li", "ul", "ol", "section", "article", "blockquote"}
        return not any(
            getattr(child, "name", None) in block_tags
            for child in el.children
        )

    text_blocks: list[str] = []

    # Pass 1: <p> tags
    for p in about.find_all("p"):
        text = p.get_text(separator=" ", strip=True)
        if text:
            text_blocks.append(text)

    # Pass 2: if no <p> results, try leaf <div> / <span> / <li>
    if not text_blocks:
        for tag in about.find_all(["div", "span", "li", "td"]):
            if _is_leaf_block(tag):
                text = tag.get_text(separator=" ", strip=True)
                if len(text) > 20:
                    text_blocks.append(text)

    # Pass 3: last resort — split raw text into lines
    if not text_blocks:
        raw = about.get_text(separator="\n", strip=True)
        text_blocks = [ln.strip() for ln in raw.split("\n") if len(ln.strip()) > 20]

    # ── Separate description from product categories ──────────────────────────
    description_parts: list[str] = []
    categories_line = ""

    for text in text_blocks:
        words = text.split()
        comma_count = text.count(",")
        is_category_line = (
            comma_count >= 2
            and comma_count / max(len(words), 1) > 0.20
            and len(text) < 800
            and not categories_line
        )
        if is_category_line:
            categories_line = text
        else:
            description_parts.append(text)

    result["information"] = " ".join(description_parts).strip()
    result["product_categories"] = categories_line

    # ── Explicit label-based category search (overrides heuristic if found) ───
    if not categories_line:
        label_patterns = re.compile(
            r"product\s*categor|categor|product\s*line|product\s*type",
            re.IGNORECASE,
        )
        for label_node in about.find_all(string=label_patterns):
            parent = label_node.parent
            if not parent:
                continue
            # Try next sibling of parent
            sib = parent.find_next_sibling()
            if sib:
                text = sib.get_text(strip=True)
                if text:
                    result["product_categories"] = text
                    break
            # Try: parent contains both label + value (strip the label prefix)
            full_text = parent.get_text(strip=True)
            stripped = label_patterns.sub("", full_text).lstrip(":- ").strip()
            if stripped and len(stripped) > 3:
                result["product_categories"] = stripped
                break


def _parse_contact_tab(soup: BeautifulSoup, result: dict) -> None:
    """
    Extract company URL, per-platform social links, and country from the
    Contact tab (and/or About tab).  If no contact tab is found, falls back
    to scanning the whole page so data is never silently dropped.
    """
    contact = _find_tab(soup, [
        "[id$='_contact']",
        "[id*='contact'][class*='tab']",
        "[id*='contact'][class*='pane']",
        "#tab-contact",
        "#contact",
        "[data-tab='contact']",
        "[aria-label*='contact' i]",
    ])

    # If contact tab not found, scan the entire page body as fallback
    scan_root = contact if contact else soup

    # ── Scan for social links + company URL ───────────────────────────────────
    _collect_links(scan_root, result)

    # Also check About tab for social links (some sites put them there)
    about = _find_tab(soup, [
        "[id$='_about']", "[id*='about'][class*='tab']",
        "[id*='about'][class*='pane']", "#about",
    ])
    if about and about is not scan_root:
        _collect_links(about, result, social_only=True)

    # Whole-page fallback: scan entire body if still missing key fields
    missing_social = any(
        not result[f"{p}_url"]
        for p in _PLATFORM_DOMAINS
    )
    if missing_social or not result["company_url"]:
        _collect_links(soup, result)

    # ── Country from address block ────────────────────────────────────────────
    address_text = _extract_address_text(contact if contact else soup)
    if address_text:
        result["country"] = _guess_country(address_text)


def _collect_links(
    container: BeautifulSoup,
    result: dict,
    social_only: bool = False,
) -> None:
    """
    Walk <a href> elements in container.
    - Assign social URLs to their per-platform fields (first occurrence wins).
    - Assign the first non-excluded, non-social external URL to company_url.

    If social_only=True, skip the company_url assignment.
    """
    for anchor in container.find_all("a", href=True):
        href: str = anchor["href"]
        if not href.startswith("http"):
            continue

        platform = _classify_social_url(href)
        if platform:
            field = f"{platform}_url"
            if not result.get(field):
                result[field] = href
            continue

        if social_only:
            continue

        parsed = urlparse(href)
        domain = parsed.netloc.lower().lstrip("www.")
        if any(excl in domain for excl in config.EXCLUDED_DOMAINS):
            continue

        if not result["company_url"]:
            # Prefer links labelled "website" / "homepage" over generic first link
            anchor_text = anchor.get_text(strip=True).lower()
            parent_text = (
                anchor.parent.get_text(strip=True).lower()
                if anchor.parent else ""
            )
            is_labelled = any(
                kw in anchor_text + " " + parent_text
                for kw in ("website", "web site", "homepage", "home page", "visit us")
            )
            if is_labelled:
                result["company_url"] = href
            elif not result["company_url"]:
                # Store as candidate but keep scanning for a labelled one
                result["company_url"] = href


def _extract_address_text(container: BeautifulSoup) -> str:
    """Find address-like text in a contact container."""
    # Try explicit <address> tag first
    addr = container.find("address")
    if addr:
        return addr.get_text(separator="\n", strip=True)

    # Try common classes
    for sel in (
        ".contact-info", ".address", "[class*='address']",
        "[class*='location']", "[class*='contact-details']",
    ):
        el = container.select_one(sel)
        if el:
            return el.get_text(separator="\n", strip=True)

    # Fallback: all text in the contact tab
    return container.get_text(separator="\n", strip=True)


def _guess_country(address_text: str) -> str:
    """
    Best-effort country extraction from an address block.
    Looks for the last non-empty line (common in international address formats).
    """
    lines = [ln.strip() for ln in address_text.split("\n") if ln.strip()]
    if not lines:
        return ""

    last_line = lines[-1]

    # If last line looks like a US ZIP or ZIP+4, try second-to-last
    if re.match(r"^\d{5}(-\d{4})?$", last_line) and len(lines) >= 2:
        last_line = lines[-2]

    country = last_line.rstrip(".,;")

    # Sanity check: if very long it's probably not just a country name
    if len(country) > 60:
        return ""

    return country


# ── Team members ───────────────────────────────────────────────────────────────

def extract_team_members(html: str, exhibitor_name: str) -> list[dict]:
    """
    Attempt to extract team member records from a public exhibitor page.
    Returns [{exhibitor_name, team_member_name, job_title}].
    Returns an empty list gracefully if team data is login-gated or absent.

    Caller should click the team tab before getting the HTML so that AJAX
    content is rendered.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[dict] = []

    card_selectors = [
        # SmallWorld Labs-specific
        ".swl-member",
        ".swl-person",
        "[class*='swl-member']",
        "[class*='member-row']",
        "[class*='person-row']",
        ".org-member",
        "[class*='org-member']",
        # Team tab content
        "[id$='_team'] [class*='member']",
        "[id$='_team'] li",
        "[id*='team'] [class*='member']",
        "[id*='team'] li",
        "[id$='_people'] li",
        "[id$='_staff'] li",
        "[id*='people'] li",
        "[id*='staff'] li",
        # Profile / people cards
        ".member-card",
        ".staff-item",
        ".people-card",
        ".team-member",
        ".contact-card",
        ".profile-card",
        ".person-card",
        # WordPress team plugins
        ".team-member-entry",
        ".staff-member",
        "[class*='team-member']",
        # Generic list items in member/team/people/staff sections
        "[class*='member-list'] li",
        "[class*='team-members'] li",
        "[class*='staff-list'] li",
        "[class*='people-list'] li",
        # Table-based layouts
        ".members-table tr:not(:first-child)",
        "table[class*='team'] tr:not(:first-child)",
    ]

    name_selectors = [
        ".member-name", ".staff-name", ".person-name", ".contact-name",
        ".swl-member-name", "[class*='member-name']", "[class*='person-name']",
        "h4", "h5", "h6", "strong", ".name", "[class*='name']",
    ]
    title_selectors = [
        ".member-title", ".job-title", ".staff-role", ".person-title",
        ".swl-member-title", "[class*='job-title']", "[class*='member-title']",
        ".role", ".position", "em", "small", "span[class*='title']",
        "[class*='title']", "[class*='role']", "[class*='position']",
    ]

    for selector in card_selectors:
        try:
            cards = soup.select(selector)
        except Exception:
            continue
        if not cards:
            continue

        for card in cards:
            name = _extract_text_by_selectors(card, name_selectors)
            title = _extract_text_by_selectors(card, title_selectors)
            if name:
                results.append({
                    "exhibitor_name": exhibitor_name,
                    "team_member_name": name,
                    "job_title": title,
                })

        if results:
            break   # Found members with this selector; stop trying others

    if not results:
        logger.debug(f"No public team members found for '{exhibitor_name}'")

    return results


def _extract_text_by_selectors(container, selectors: list[str]) -> str:
    """Try each CSS selector; return the first non-empty text found."""
    for sel in selectors:
        el = container.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text:
                return text
    return ""
