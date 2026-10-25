# stores.py
"""
Direct store search for price comparison (Indian stores, prices in INR).

Each adapter searches one store for the product and returns candidate
listings as {"title", "price", "mrp", "url", "in_stock"}. prices.py decides
which candidates really are the product.

Store pages change often, so every adapter fails quietly and returns []:
a broken store simply drops out of the comparison.
"""

import re
import json
import logging
from urllib.parse import quote_plus, urlparse, parse_qs

from bs4 import BeautifulSoup

from scraper import browser_get

log = logging.getLogger(__name__)

RUPEE = chr(0x20B9)
RUPEE_AMOUNT = re.compile(RUPEE + r"\s*([\d,]+(?:\.\d+)?)")
MRP_AMOUNT = re.compile(r"MRP[^\d]{0,12}([\d,]+(?:\.\d+)?)", re.IGNORECASE)
SOLD_OUT = re.compile(r"out of stock|sold out|currently unavailable|coming soon", re.IGNORECASE)


def _amount(value) -> float | None:
    """'1,299.00' / 'Rs. 1,299' / 1299 -> 1299.0"""
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    match = re.search(r"\d[\d,]*(?:\.\d+)?", value)
    if not match:
        return None
    amount = float(match.group().replace(",", ""))
    return amount if amount > 0 else None


def _listing(title, price, mrp, url, in_stock) -> dict | None:
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    price = _amount(price)
    mrp = _amount(mrp)
    if not title or not price or not url:
        return None
    return {
        "title": title,
        "price": price,
        "mrp": mrp if mrp and mrp > price else None,
        "url": url,
        "in_stock": in_stock,
    }


def _soup(url: str) -> BeautifulSoup | None:
    r = browser_get(url)
    return BeautifulSoup(r.text, "lxml") if r is not None else None


# ── Adapters ──────────────────────────────────────────────────
def search_amazon(query: str) -> list[dict]:
    soup = _soup(f"https://www.amazon.in/s?k={quote_plus(query)}")
    if soup is None:
        return []
    out = []
    for item in soup.select('div[data-component-type="s-search-result"]'):
        asin = item.get("data-asin")
        sponsored = "AdHolder" in item.get("class", []) or item.select_one(".puis-sponsored-label-text")
        if not asin or sponsored:
            continue
        title_el = item.select_one('[data-cy="title-recipe"]') or item.select_one("h2")
        image = item.select_one("img.s-image")
        title = title_el.get_text(" ", strip=True) if title_el else (image.get("alt") if image else "")
        price = item.select_one(".a-price:not(.a-text-price) .a-offscreen")
        mrp = item.select_one(".a-price.a-text-price .a-offscreen")
        listing = _listing(
            title,
            price.get_text() if price else None,
            mrp.get_text() if mrp else None,
            f"https://www.amazon.in/dp/{asin}",
            True,  # Amazon only shows a price for listings that can be bought
        )
        if listing:
            out.append(listing)
    return out


def search_flipkart(query: str) -> list[dict]:
    soup = _soup(f"https://www.flipkart.com/search?q={quote_plus(query)}")
    if soup is None:
        return []

    def key(href):
        # Colour variants share a URL path and differ only by pid.
        parsed = urlparse(href)
        return parsed.path, parse_qs(parsed.query).get("pid", [""])[0]

    out, seen = [], set()
    for link in soup.select('a[href*="/p/itm"]'):
        href = link.get("href", "")
        k = key(href)
        if k in seen:
            continue
        seen.add(k)
        # Climb to the largest element that still holds only this product: its card.
        card = link
        while card.parent is not None and len(
            {key(a.get("href", "")) for a in card.parent.select('a[href*="/p/itm"]')}
        ) <= 1:
            card = card.parent
        text = card.get_text(" ", strip=True)
        amounts = RUPEE_AMOUNT.findall(text)
        if not amounts:
            continue
        title = next((e["title"] for e in card.select("[title]") if e.get("title")), None) or next(
            (img["alt"] for img in card.select("img[alt]") if img.get("alt")), ""
        )
        path, pid = k
        listing = _listing(
            title,
            amounts[0],
            amounts[1] if len(amounts) > 1 else None,
            f"https://www.flipkart.com{path}" + (f"?pid={pid}" if pid else ""),
            not SOLD_OUT.search(text),
        )
        if listing:
            out.append(listing)
    return out


def search_croma(query: str) -> list[dict]:
    r = browser_get(
        "https://api.croma.com/searchservices/v1/search?currentPage=0"
        f"&query={quote_plus(query)}%3Arelevance&fields=FULL&channel=WEB"
        "&channelCode=400049&spellOpt=DEFAULT"
    )
    if r is None:
        return []
    out = []
    for p in r.json().get("products") or []:
        listing = _listing(
            p.get("name"),
            (p.get("price") or {}).get("value"),
            (p.get("mrp") or {}).get("value"),
            "https://www.croma.com" + p["url"] if p.get("url") else None,
            None,
        )
        if listing:
            out.append(listing)
    return out


def search_reliance_digital(query: str) -> list[dict]:
    soup = _soup(f"https://www.reliancedigital.in/products?q={quote_plus(query)}")
    if soup is None:
        return []
    out, seen = [], set()
    for link in soup.select('a[href*="/product/"]'):
        href = link.get("href", "")
        text = link.get_text(" | ", strip=True)
        if href in seen or RUPEE not in text:
            continue
        seen.add(href)
        mrp = MRP_AMOUNT.search(text)
        listing = _listing(
            text.split(" | ")[0],
            RUPEE_AMOUNT.search(text).group(1),
            mrp.group(1) if mrp else None,
            href if href.startswith("http") else f"https://www.reliancedigital.in{href}",
            not SOLD_OUT.search(text),
        )
        if listing:
            out.append(listing)
    return out


def search_myntra(query: str) -> list[dict]:
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
    r = browser_get(f"https://www.myntra.com/{slug}")
    if r is None:
        return []
    match = re.search(r"window\.__myx\s*=\s*(\{.*?\})\s*</script>", r.text, re.S)
    if not match:
        return []
    products = json.loads(match.group(1)).get("searchData", {}).get("results", {}).get("products") or []
    out = []
    for p in products:
        inventory = p.get("inventoryInfo") or []
        listing = _listing(
            p.get("productName"),
            p.get("price"),
            p.get("mrp"),
            f"https://www.myntra.com/{p['landingPageUrl']}" if p.get("landingPageUrl") else None,
            any(i.get("available") for i in inventory) if inventory else None,
        )
        if listing:
            out.append(listing)
    return out


def search_snapdeal(query: str) -> list[dict]:
    soup = _soup(f"https://www.snapdeal.com/search?keyword={quote_plus(query)}")
    if soup is None:
        return []
    out = []
    for item in soup.select(".product-tuple-listing"):
        title = item.select_one(".product-title")
        price = item.select_one(".product-price")
        mrp = item.select_one(".product-desc-price")
        link = item.select_one("a.dp-widget-link")
        listing = _listing(
            (title.get("title") or title.get_text(strip=True)) if title else None,
            (price.get("display-price") or price.get_text()) if price else None,
            mrp.get_text() if mrp else None,
            link.get("href") if link else None,
            not SOLD_OUT.search(item.get_text(" ", strip=True)),
        )
        if listing:
            out.append(listing)
    return out


# ── Registry ──────────────────────────────────────────────────
# key: (display name, domain, adapter)
STORES = {
    "amazon": ("Amazon", "amazon.in", search_amazon),
    "flipkart": ("Flipkart", "flipkart.com", search_flipkart),
    "croma": ("Croma", "croma.com", search_croma),
    "reliance_digital": ("Reliance Digital", "reliancedigital.in", search_reliance_digital),
    "myntra": ("Myntra", "myntra.com", search_myntra),
    "snapdeal": ("Snapdeal", "snapdeal.com", search_snapdeal),
}

# Friendly names for listings found through web search as well.
STORE_NAMES = {domain: name for name, domain, _ in STORES.values()}


def store_name(domain: str) -> str:
    return STORE_NAMES.get(domain, domain)


def search_store(key: str, query: str) -> list[dict]:
    """Run one store adapter; returns [] if the store is unknown or fails."""
    if key not in STORES:
        log.warning("unknown store in PRICE_STORES: %s", key)
        return []
    name, domain, adapter = STORES[key]
    try:
        listings = adapter(query)
    except Exception as e:  # layout changes, JSON errors, timeouts
        log.info("store search failed for %s: %s", name, e)
        return []
    for listing in listings:
        listing["store"] = name
        listing["domain"] = domain
    return listings
