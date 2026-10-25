# scraper.py
"""
Web scraping module.
- Searches the web for review pages of a product (ddgs meta-search,
  falling back to the DuckDuckGo HTML endpoint).
- Downloads the pages in parallel, extracts review-like text and user ratings.
- Falls back to parsing manually-pasted reviews.
"""

import re
import json
import time
import logging
import threading
import statistics
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

import config

try:
    from ddgs import DDGS
except ImportError:  # optional: fall back to plain HTML search
    DDGS = None

try:
    import primp  # browser-impersonating HTTP client (installed with ddgs)
except ImportError:
    primp = None

log = logging.getLogger(__name__)

# ── HTTP session ──────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Sites whose pages are video, social or login walls with no review text.
SKIP_DOMAINS = {
    "youtube.com", "youtu.be", "facebook.com", "instagram.com",
    "x.com", "twitter.com", "tiktok.com", "pinterest.com", "linkedin.com",
}


def _is_useful_url(url: str) -> bool:
    if not url.startswith("http") or url.lower().endswith(".pdf"):
        return False
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return not any(host == d or host.endswith("." + d) for d in SKIP_DOMAINS)


# ── 1. Search ─────────────────────────────────────────────────
# Parallel ddgs queries (reviews + prices) often come back empty, so searches
# take turns. Page downloads still run in parallel.
_SEARCH_LOCK = threading.Lock()


def _search_ddgs(query: str, num_results: int, region: str = config.SEARCH_REGION) -> list[str]:
    """
    Meta-search via the ddgs package (queries several engines).
    Search engines rate-limit automated queries now and then, so a failed
    search is retried with a growing pause.
    """
    if DDGS is None:
        return []
    for attempt in range(1, config.SEARCH_ATTEMPTS + 1):
        try:
            with _SEARCH_LOCK:
                results = DDGS().text(
                    query,
                    region=region,
                    max_results=num_results,
                    backend=config.SEARCH_BACKENDS,
                )
            return [r.get("href", "") for r in results]
        except Exception as e:
            log.warning("ddgs search failed (attempt %d): %s", attempt, e)
            if attempt < config.SEARCH_ATTEMPTS:
                time.sleep(2 * attempt)
    return []


def _search_ddg_html(query: str, num_results: int) -> list[str]:
    """Plain DuckDuckGo HTML search. Often blocked, so used only as a fallback."""
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        r = SESSION.get(url, timeout=config.FETCH_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        log.warning("DuckDuckGo HTML search failed: %s", e)
        return []

    soup = BeautifulSoup(r.text, "lxml")
    urls = []
    for a in soup.select("a.result__a")[:num_results]:
        href = a.get("href", "")
        # DDG wraps links in a redirect: extract the real URL
        if "uddg=" in href:
            qs = parse_qs(urlparse(href).query)
            href = qs.get("uddg", [href])[0]
        urls.append(href)
    return urls


def search_web(query: str, num_results: int = config.SEARCH_RESULTS,
               region: str = config.SEARCH_REGION) -> list[str]:
    """Return up to `num_results` useful result URLs for a query."""
    # Ask for a few extra results because some are filtered out below.
    found = _search_ddgs(query, num_results + 4, region) or _search_ddg_html(query, num_results + 4)

    urls = []
    for url in found:
        if _is_useful_url(url) and url not in urls:
            urls.append(url)
    return urls[:num_results]


def search_reviews(product_name: str, num_results: int = config.SEARCH_RESULTS) -> list[str]:
    """Find review-page URLs for the product."""
    return search_web(f"{product_name} user reviews", num_results)


# ── 2. Fetch ──────────────────────────────────────────────────
def browser_get(url: str):
    """
    GET with a real browser's TLS fingerprint. Stores such as Flipkart and
    Croma reject plain Python clients with 403 but serve this normally.
    Returns the response, or None on failure.
    """
    if primp is None:
        return None
    try:
        client = primp.Client(
            impersonate="random",
            impersonate_os="windows",
            timeout=config.FETCH_TIMEOUT,
            follow_redirects=True,
        )
        r = client.get(url)
    except Exception as e:
        log.info("browser fetch failed %s: %s", url, e)
        return None
    return r if r.status_code == 200 else None


def fetch_page(url: str) -> str:
    """Download page HTML. Returns '' on any failure."""
    try:
        r = SESSION.get(url, timeout=config.FETCH_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        log.info("fetch failed %s: %s", url, e)
        # Blocked or rate-limited: try again looking like a browser.
        blocked = getattr(getattr(e, "response", None), "status_code", None) in (403, 429, 503)
        r = browser_get(url) if blocked else None
        if r is None:
            return ""
        return r.text if "html" in r.headers.get("content-type", "html") else ""

    if "html" not in r.headers.get("Content-Type", "html"):
        return ""
    # requests assumes ISO-8859-1 when the server sends no charset,
    # which garbles curly quotes and currency symbols. Most pages are UTF-8.
    if "charset" not in r.headers.get("Content-Type", "").lower():
        r.encoding = "utf-8"
    return r.text


def fetch_pages(urls: list[str]) -> list[str]:
    """Download several pages in parallel, preserving order."""
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=config.FETCH_WORKERS) as pool:
        return list(pool.map(fetch_page, urls))


# ── 3. Extract reviews ────────────────────────────────────────
# Phrases that mark cookie banners, login dialogs, ads and other page chrome.
NOISE_PATTERN = re.compile(
    r"\b(cookies?|privacy policy|terms of (?:service|use)|terms and conditions|"
    r"sign up|sign in|log ?in|subscribe|newsletter|advertisement|otp|"
    r"all rights reserved|copyright|affiliate links?|download (?:the|our) app)\b",
    re.IGNORECASE,
)

REVIEW_SELECTORS = ", ".join([
    '[class*="review"]', '[class*="Review"]',
    '[class*="comment"]', '[class*="Comment"]',
    '[class*="testimonial"]', '[class*="feedback"]',
    '[class*="opinion"]',
])

# Keep taking paragraphs until a page yields at least this many snippets.
MIN_SNIPPETS_PER_PAGE = 5


def _is_valid_review(text: str) -> bool:
    if not text or len(text) < config.MIN_CHARS_PER_REVIEW or len(text) > 2000:
        return False
    if len(text.split()) < 8:
        return False
    if NOISE_PATTERN.search(text):
        return False
    return True


def _reviews_from_json_ld(soup: BeautifulSoup) -> list[str]:
    """Collect reviewBody text from schema.org Review objects in JSON-LD."""
    found: list[str] = []

    def walk(data):
        if isinstance(data, dict):
            body = data.get("reviewBody")
            if isinstance(body, str):
                found.append(body)
            for v in data.values():
                walk(v)
        elif isinstance(data, list):
            for item in data:
                walk(item)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(script.string or ""))
        except (json.JSONDecodeError, TypeError):
            continue
    return found


def _innermost_matches(soup: BeautifulSoup, selector: str) -> list[str]:
    """
    Text of elements matching `selector` that look like a review and do not
    contain a smaller element that also does. Outer wrappers such as a
    "reviews-section" would otherwise swallow every review into one block,
    while tiny matches like a "review-like-count" span must not hide the
    container that holds the actual review text.
    """
    texts = {}
    for el in soup.select(selector):
        text = clean_text(el.get_text(" ", strip=True))
        if _is_valid_review(text):
            texts[id(el)] = (el, text)

    return [
        text
        for el, text in texts.values()
        if not any(id(child) in texts for child in el.find_all(True))
    ]


def extract_reviews(soup: BeautifulSoup) -> list[str]:
    """
    Pull review-like text from a parsed page, most reliable sources first.
    Removes page chrome (nav, footer, scripts, ...) from `soup` as it goes.
    """
    # Strategy 1: structured data (JSON-LD, then microdata)
    candidates = _reviews_from_json_ld(soup)
    candidates += [
        el.get_text(" ", strip=True)
        for el in soup.select('[itemprop="reviewBody"]')
    ]

    # Strip noise before looking at visible markup
    for tag in soup(["script", "style", "nav", "footer", "header",
                     "aside", "form", "noscript", "iframe", "button", "svg"]):
        tag.decompose()

    # Strategy 2: review-ish class names
    candidates += _innermost_matches(soup, REVIEW_SELECTORS)

    # Strategy 3: paragraphs / list items (expert reviews, forums, blogs)
    if sum(map(_is_valid_review, candidates)) < MIN_SNIPPETS_PER_PAGE:
        candidates += [p.get_text(" ", strip=True) for p in soup.find_all(["p", "li"])]

    # Clean, filter, and deduplicate (first 100 chars as key)
    seen, unique = set(), []
    for text in candidates:
        text = clean_text(text)
        if not _is_valid_review(text):
            continue
        key = text[:100].lower()
        if key not in seen:
            seen.add(key)
            unique.append(text)
    return unique


# ── 4. Extract user rating ────────────────────────────────────
def _to_five_star(value, best=None):
    """Convert a rating to a 0-5 scale. Returns None if it is not a number."""
    try:
        value = float(value)
        best = float(best) if best not in (None, "") else 5.0
    except (ValueError, TypeError):
        return None
    if best <= 0:
        return None
    if best != 5.0:
        value = value / best * 5
    return value if 0 < value <= 5 else None


def _is_about(product_name: str | None, item_name: str) -> bool:
    """Whether a structured-data item is the product, not another one on the page."""
    if not product_name or not item_name:
        return True  # nothing to compare against
    from prices import matches_product  # imported here: prices imports this module
    return matches_product(product_name, item_name)


def _find_rating_in_json(data, product_name: str | None = None):
    """
    Recursively search JSON-LD for the product's aggregateRating.ratingValue.
    Ratings of other items (sidebar products, comparison pages) are skipped.
    """
    if isinstance(data, dict):
        ar = data.get("aggregateRating")
        if isinstance(ar, dict) and "ratingValue" in ar and _is_about(product_name, str(data.get("name") or "")):
            r = _to_five_star(ar["ratingValue"], ar.get("bestRating"))
            if r is not None:
                return r
        for v in data.values():
            r = _find_rating_in_json(v, product_name)
            if r is not None:
                return r
    elif isinstance(data, list):
        for item in data:
            r = _find_rating_in_json(item, product_name)
            if r is not None:
                return r
    return None


RATING_PATTERNS = [
    re.compile(r"(\d\.\d)\s*(?:out of|/)\s*5\b", re.IGNORECASE),
    re.compile(r"rating[:\s]+(\d\.\d)\b", re.IGNORECASE),
    re.compile(r"(\d\.\d)\s*stars?\b", re.IGNORECASE),
]


def structured_rating(soup: BeautifulSoup, product_name: str | None = None):
    """The product's rating from JSON-LD, as float or None. Needs the page's scripts intact."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        r = _find_rating_in_json(data, product_name)
        if r is not None:
            return round(r, 2)
    return None


def text_rating(soup: BeautifulSoup):
    """
    A rating written in the page text ("4.2 out of 5", "4.2/5", "rating: 4.2",
    "4.2 stars"), or None. Less reliable than structured data, so run it after
    extract_reviews has removed menus and sidebars that list other products.
    """
    text = soup.get_text(" ", strip=True)
    for pattern in RATING_PATTERNS:
        m = pattern.search(text)
        if m:
            r = _to_five_star(m.group(1))
            if r is not None:
                return round(r, 2)
    return None


def read_review_page(url: str, product_name: str | None = None) -> dict:
    """
    Download one page and extract its reviews and rating from a single parse.
    Returns {"reviews": [...], "rating": float | None, "structured": bool}.
    """
    empty = {"reviews": [], "rating": None, "structured": False}
    html = fetch_page(url)
    if not html:
        return empty
    try:
        soup = BeautifulSoup(html, "lxml")
        rating = structured_rating(soup, product_name)
        reviews = extract_reviews(soup)
        structured = rating is not None
        if not structured:
            rating = text_rating(soup)
    except Exception as e:  # one odd page must not break the scrape
        log.info("extraction failed for %s: %s", url, e)
        return empty
    return {"reviews": reviews, "rating": rating, "structured": structured}


# ── 5. Clean text ─────────────────────────────────────────────
# Pictographs, dingbats and emoji joiners. Star characters (U+2605/2606)
# are kept because reviewers use them to express ratings.
EMOJI_PATTERN = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u2604\u2607-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+"
)


def clean_text(text: str) -> str:
    text = re.sub(r"http\S+", "", text)
    text = EMOJI_PATTERN.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _truncate(text: str, limit: int = config.MAX_CHARS_PER_REVIEW) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


# ── 6. Main orchestrator ──────────────────────────────────────
def scrape_reviews(product_name: str, max_reviews: int = config.DEFAULT_REVIEWS_TO_SCRAPE,
                   progress=None) -> dict:
    """
    Search, download and extract reviews for a product.

    `progress` is an optional callable that receives short status messages.

    Returns:
      {
        "product_name": str,
        "reviews": [str, ...],
        "review_sources": [url, ...],   # page each review came from
        "user_rating": float | None,
        "rating_pages": int,            # pages the rating was read from
        "sources": [url, ...],
        "error": str | None,
      }
    """
    report = progress or (lambda msg: None)
    out = {
        "product_name": product_name,
        "reviews": [],
        "review_sources": [],
        "user_rating": None,
        "rating_pages": 0,
        "sources": [],
        "error": None,
    }

    report("Searching the web for review pages")
    urls = search_reviews(product_name)
    if not urls:
        out["error"] = "No review pages found. The search may be rate-limited; try again or paste reviews manually."
        return out

    # Each worker downloads and parses its page, so parsing overlaps downloads.
    report(f"Reading {len(urls)} review pages")
    with ThreadPoolExecutor(max_workers=config.FETCH_WORKERS) as pool:
        pages = list(pool.map(lambda url: read_review_page(url, product_name), urls))

    per_source = [(url, page["reviews"]) for url, page in zip(urls, pages) if page["reviews"]]
    # Ratings from structured data are checked against the product name.
    # Ratings spotted in page text are used only when there are none of those,
    # and only if at least two pages show one: a lone match is often another
    # product's rating.
    structured = [p["rating"] for p in pages if p["structured"]]
    in_text = [p["rating"] for p in pages if p["rating"] is not None and not p["structured"]]
    ratings_found = structured or (in_text if len(in_text) >= 2 else [])

    # Take reviews round-robin across sources so one long article
    # cannot fill the whole quota.
    seen = set()
    depth = max((len(reviews) for _, reviews in per_source), default=0)
    for i in range(depth):
        for url, reviews in per_source:
            if len(out["reviews"]) >= max_reviews:
                break
            if i >= len(reviews):
                continue
            key = reviews[i][:100].lower()
            if key in seen:
                continue
            seen.add(key)
            out["reviews"].append(_truncate(reviews[i]))
            out["review_sources"].append(url)
            if url not in out["sources"]:
                out["sources"].append(url)

    if ratings_found:
        # Median, so one odd page (an editorial score, a different variant) cannot skew it.
        out["user_rating"] = round(statistics.median(ratings_found), 2)
        out["rating_pages"] = len(ratings_found)

    if not out["reviews"]:
        out["error"] = "No usable reviews could be extracted. Try pasting reviews manually."

    return out


# ── 7. Manual paste parser ────────────────────────────────────
def parse_manual_reviews(text: str) -> list[str]:
    """Split pasted text into individual reviews (blank-line or numbered)."""
    parts = re.split(r"\n\s*\n|\n(?=\s*\d+[\.\)]\s)", text)
    reviews = []
    for p in parts:
        p = clean_text(re.sub(r"^\s*\d+[\.\)]\s*", "", p))
        if len(p) >= config.MIN_CHARS_PER_REVIEW:
            reviews.append(_truncate(p))
    return reviews
