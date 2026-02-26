"""
HTML parsing and data extraction functions.
All functions accept rendered HTML strings and return clean Python dicts/lists.
No browser dependencies here — pure BeautifulSoup.

Confirmed selectors from live analysis of expowest26.smallworldlabs.com (Feb 2026):
  - Cards link to /co/{slug}
  - Company name: #organizations_profile_0_0 h4
  - Booth:        <a href="...MapItBooth=3312...">Booth #3312</a>
  - About tab:    [id$='_about']  → description paragraphs + categories line
  - Contact tab:  [id$='_contact'] → address block + website link
  - Hall:         NOT publicly exposed (left blank)
  - Social links: best-effort from about + contact tab anchors
"""
import logging
import re
from urllib.parse import urlparse, parse_qs, unquote

from bs4 import BeautifulSoup

import config

logger = logging.getLogger("expowest_scraper.extractors")


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
    for el in soup.select(".pagination a, .page-numbers, [class*='paginator'] a"):
        text = el.get_text(strip=True)
        if text.isdigit():
            max_page = max(max_page, int(text))

    logger.debug(f"Detected {max_page} total pages")
    return max_page


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
        "exhibitor_name": "",
        "booth_number": "",
        "information": "",
        "product_categories": "",
        "hall": "",          # not publicly available on SmallWorld Labs
        "country": "",
        "company_url": "",
        "social_media_links": "",
        "source_url": source_url,
    }

    try:
        _parse_name(soup, result)
        _parse_booth(soup, result)
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

    # Fallback: first <h1> or <h2> on page
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


def _parse_about_tab(soup: BeautifulSoup, result: dict) -> None:
    about = soup.select_one("[id$='_about']")
    if not about:
        return

    paragraphs = about.find_all("p")
    description_parts: list[str] = []
    categories_line = ""

    for p in paragraphs:
        text = p.get_text(separator=" ", strip=True)
        if not text:
            continue
        words = text.split()
        comma_count = text.count(",")
        # Heuristic: a categories line has many commas relative to word count
        is_categories = (
            comma_count >= 2
            and comma_count / max(len(words), 1) > 0.25
            and len(text) < 600
            and not categories_line
        )
        if is_categories:
            categories_line = text
        else:
            description_parts.append(text)

    result["information"] = " ".join(description_parts).strip()
    result["product_categories"] = categories_line

    # If heuristic missed, look for a label element near the category data
    if not categories_line:
        for label in about.find_all(string=re.compile(r"categor", re.I)):
            parent = label.parent
            if parent:
                sibling = parent.find_next_sibling()
                if sibling:
                    result["product_categories"] = sibling.get_text(strip=True)
                    break


def _parse_contact_tab(soup: BeautifulSoup, result: dict) -> None:
    contact = soup.select_one("[id$='_contact']")
    if not contact:
        return

    social_links: list[str] = []

    for anchor in contact.find_all("a", href=True):
        href: str = anchor["href"]
        if not href.startswith("http"):
            continue

        parsed = urlparse(href)
        domain = parsed.netloc.lower().lstrip("www.")

        # Social media
        if any(s in domain for s in config.SOCIAL_DOMAINS):
            if href not in social_links:
                social_links.append(href)
            continue

        # Skip internal / event platform links
        if any(excl in domain for excl in config.EXCLUDED_DOMAINS):
            continue

        # First external link = company website
        if not result["company_url"]:
            result["company_url"] = href

    # Also scan the about tab for social links
    about = soup.select_one("[id$='_about']")
    if about:
        for anchor in about.find_all("a", href=True):
            href = anchor["href"]
            if not href.startswith("http"):
                continue
            parsed = urlparse(href)
            domain = parsed.netloc.lower().lstrip("www.")
            if any(s in domain for s in config.SOCIAL_DOMAINS):
                if href not in social_links:
                    social_links.append(href)

    result["social_media_links"] = " | ".join(social_links)

    # Country: look for an address block; country is typically the last non-empty line
    address_text = _extract_address_text(contact)
    if address_text:
        result["country"] = _guess_country(address_text)


def _extract_address_text(container: BeautifulSoup) -> str:
    """Find address-like text in a contact container."""
    # Try explicit <address> tag first
    addr = container.find("address")
    if addr:
        return addr.get_text(separator="\n", strip=True)

    # Try common classes
    for sel in (".contact-info", ".address", "[class*='address']", "[class*='location']"):
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

    # The last line often contains country or ZIP + country
    last_line = lines[-1]

    # If last line looks like a US ZIP or ZIP+4, try second-to-last
    if re.match(r"^\d{5}(-\d{4})?$", last_line) and len(lines) >= 2:
        last_line = lines[-2]

    # Strip trailing punctuation
    country = last_line.rstrip(".,;")

    # Simple sanity check: if it's very long it's probably not just a country name
    if len(country) > 60:
        return ""

    return country


# ── Team members ───────────────────────────────────────────────────────────────

def extract_team_members(html: str, exhibitor_name: str) -> list[dict]:
    """
    Attempt to extract team member records from a public exhibitor page.
    Returns a list of dicts: [{exhibitor_name, team_member_name, job_title}].
    Returns an empty list gracefully if team data is login-gated or absent.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[dict] = []

    # Try various selectors used by SmallWorld Labs / WordPress member plugins
    card_selectors = [
        ".member-card",
        ".staff-item",
        ".people-card",
        "[class*='member-list'] li",
        "[class*='team-members'] li",
        "[class*='staff-list'] li",
        ".org-member",
    ]

    for selector in card_selectors:
        cards = soup.select(selector)
        if not cards:
            continue

        for card in cards:
            name = _extract_text_by_selectors(card, [
                ".member-name", ".staff-name", "h5", "h4",
                "[class*='name']", "strong",
            ])
            title = _extract_text_by_selectors(card, [
                ".member-title", ".job-title", ".staff-role",
                "[class*='title']", "[class*='role']", "em", "small",
            ])
            if name:
                results.append({
                    "exhibitor_name": exhibitor_name,
                    "team_member_name": name,
                    "job_title": title,
                })

        if results:
            break  # Found members with this selector; stop trying others

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
