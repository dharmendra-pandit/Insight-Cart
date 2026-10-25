# prices.py
"""
Price comparison module.
- Searches stores directly (Amazon, Flipkart, Croma, ... see stores.py).
- Searches the web for other store and price-comparison pages, and extracts
  prices from structured data (JSON-LD offers, product meta tags) and from
  Amazon's price markup.
- Keeps one listing per store that matches the product, in one currency,
  sorted cheapest first.
Runs without Streamlit so it can be used from a background thread.
"""

import re
import json
import logging
from concurrent.futures import ThreadPoolExecutor, Future
from urllib.parse import urlparse, quote_plus

from bs4 import BeautifulSoup

import config
import scraper
import stores

log = logging.getLogger(__name__)

CURRENCY_SYMBOLS = {
    "INR": chr(0x20B9),
    "USD": "$",
    "EUR": chr(0x20AC),
    "GBP": chr(0x00A3),
}
SYMBOL_TO_CURRENCY = {symbol: code for code, symbol in CURRENCY_SYMBOLS.items()}
SYMBOL_TO_CURRENCY["Rs"] = "INR"

AMAZON_PRICE_SELECTORS = [
    "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
    "#corePrice_feature_div .a-price .a-offscreen",
    "#apex_desktop .a-price .a-offscreen",
    "#priceblock_dealprice",
    "#priceblock_ourprice",
]

# One shared worker so price lookups can run while reviews are scraped.
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="prices")


# ── Formatting ────────────────────────────────────────────────
def _indian_grouping(n: int) -> str:
    """1032000 -> '10,32,000' (lakh / crore grouping)."""
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail])


def format_price(amount, currency: str = config.PRICE_CURRENCY) -> str:
    """Human-readable price with currency symbol, e.g. 1,299 or 10,32,000 for INR."""
    if amount is None:
        return "N/A"
    symbol = CURRENCY_SYMBOLS.get(currency, f"{currency} ")
    amount = round(amount, 2)  # 39.99 - 29.99 is 10.000000000000002
    if amount < 100 and amount != int(amount):
        return f"{symbol}{amount:,.2f}"
    whole = int(round(amount))
    return f"{symbol}{_indian_grouping(whole) if currency == 'INR' else f'{whole:,}'}"


def shopping_search_url(product_name: str) -> str:
    """Google Shopping search, for when no store price could be read."""
    return f"https://www.google.com/search?tbm=shop&q={quote_plus(product_name)}"


# ── Parsing helpers ───────────────────────────────────────────
def _to_amount(value) -> float | None:
    """'1,299.00' / 'Rs. 1,299' / 1299 -> 1299.0"""
    if isinstance(value, (int, float)):
        amount = float(value)
    elif isinstance(value, str):
        cleaned = re.sub(r"[^\d.]", "", value.replace(",", ""))
        if not cleaned or cleaned.count(".") > 1:
            return None
        amount = float(cleaned)
    else:
        return None
    return amount if amount > 0 else None


def _in_stock(availability) -> bool | None:
    text = str(availability or "").lower()
    if not text:
        return None
    if any(word in text for word in ("outofstock", "soldout", "discontinued")):
        return False
    return "instock" in text or "limitedavailability" in text or None


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


# Words that, placed before the product name, mark an accessory:
# "Silicone Case for boAt Airdopes 141", "Tempered Glass iPhone 15".
ACCESSORY_WORDS = {
    "case", "cover", "skin", "protector", "guard", "tempered", "replacement",
    "compatible", "eartips", "tips", "silicone", "strap", "pouch", "sticker",
    "adapter", "mount", "holder", "spare", "for",
}
# Words that, placed after the product name, still mark an accessory:
# "iPhone 15 Back Cover", "iPhone 15 Printed Mobile Accessory". Kept narrow
# because real listings say "Aluminium Case" (watches) or "Silicone Strap".
ACCESSORY_AFTER = {
    "cover", "covers", "protector", "tempered", "skin", "pouch", "sticker",
    "accessory", "accessories", "replacement", "spare",
}
# After these words the title describes what is included, not what the item is:
# "Airdopes 141 with Charging Case".
INCLUDES_WORDS = {"with", "includes", "including", "charging", "carrying", "and", "plus"}
# Words that, right after the model number, name a different model with a
# different price: "iPhone 15 Pro", "Galaxy S24 FE", "Airdopes 141 ANC".
VARIANT_WORDS = {"pro", "plus", "max", "ultra", "lite", "mini", "fe", "se", "neo", "anc", "prime", "elite"}


def _is_accessory(product_tokens: list[str], title_tokens: list[str]) -> bool:
    """
    Accessories and spare parts name the product only as what they fit, and
    their price says nothing about the product itself.
    """
    first = next((i for i, t in enumerate(title_tokens) if t in product_tokens), None)
    if first is None:
        return False
    if set(title_tokens[:first]) & ACCESSORY_WORDS:
        return True
    for i, t in enumerate(title_tokens[first:], start=first):
        if t in INCLUDES_WORDS:
            break
        if t in ACCESSORY_AFTER or (t == "back" and title_tokens[i + 1:i + 2] == ["case"]):
            return True
    return False


def matches_product(product_name: str, listing_name: str) -> bool:
    """
    Loose match between the searched product and a listing title.
    - Each model number must directly follow the word it follows in the
      product name, with or without a space: "iPhone 15" needs "iphone 15"
      or "iphone15", so "iPhone 17e ... 15.40 cm" fails, while "SONY
      WH1000XM5" matches "Sony WH-1000XM5". "Galaxy S2" does not match
      "Galaxy S24", and "Nord Buds 2" does not match "Nord Buds 2r".
    - A variant word right after the model number ("iPhone 15 Pro",
      "Airdopes 141 ANC") must also be in the product name.
    - Most words of the product name must appear.
    - The listing must not be an accessory for the product.
    """
    wanted = _tokens(product_name)
    title = _tokens(listing_name)
    if not wanted or not title:
        return False
    text = " ".join(title)

    for i, t in enumerate(wanted):
        if any(c.isdigit() for c in t):
            anchor = re.escape(wanted[i - 1]) + " ?" if i else r"\b"
            m = re.search(anchor + re.escape(t) + r"(?![a-z0-9])(?: ([a-z]+))?", text)
            if not m:
                return False
            if m.group(1) in VARIANT_WORDS and m.group(1) not in wanted:
                return False
    if sum(t in text for t in wanted) / len(wanted) < 0.75:
        return False
    return not _is_accessory(wanted, title)


def _drop_price_outliers(offers: list[dict]) -> list[dict]:
    """
    Remove listings priced far below the rest (spare parts, EMI amounts,
    single accessories that slipped past the title check).
    """
    if len(offers) < 3:
        return offers
    ordered = sorted(o["price"] for o in offers)
    median = ordered[len(ordered) // 2]
    return [o for o in offers if o["price"] >= 0.4 * median]


def _store_name(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


# ── Extraction ────────────────────────────────────────────────
def _offers_from_json_ld(soup: BeautifulSoup, page_title: str) -> list[dict]:
    """Collect {name, low, high, currency, in_stock} from schema.org offers."""
    found = []

    def add_offer(offer, name):
        if not isinstance(offer, dict):
            return
        low = _to_amount(offer.get("price")) or _to_amount(offer.get("lowPrice"))
        high = _to_amount(offer.get("highPrice")) or low
        currency = str(offer.get("priceCurrency") or "").upper()
        if low and currency:
            found.append({
                "name": name,
                "low": low,
                "high": max(high, low),
                "currency": currency,
                "in_stock": _in_stock(offer.get("availability")),
            })

    def walk(data):
        if isinstance(data, dict):
            if "offers" in data:
                name = str(data.get("name") or page_title)
                offers = data["offers"]
                for offer in offers if isinstance(offers, list) else [offers]:
                    add_offer(offer, name)
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


def _offer_from_meta(soup: BeautifulSoup, page_title: str) -> list[dict]:
    """Open Graph / product meta tags used by Shopify and many other stores."""
    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return tag["content"]
        return None

    amount = _to_amount(meta("product:price:amount", "og:price:amount"))
    currency = (meta("product:price:currency", "og:price:currency") or "").upper()
    if not amount or not currency:
        return []
    name = meta("og:title") or page_title
    return [{"name": name, "low": amount, "high": amount, "currency": currency, "in_stock": None}]


def _offer_from_amazon(soup: BeautifulSoup) -> list[dict]:
    """Amazon product pages have no structured price, so read the price widget."""
    title_el = soup.select_one("#productTitle")
    if not title_el:
        return []
    for selector in AMAZON_PRICE_SELECTORS:
        el = soup.select_one(selector)
        text = el.get_text(strip=True) if el else ""
        amount = _to_amount(text)
        if not amount:
            continue
        symbol = next((s for s in SYMBOL_TO_CURRENCY if s in text), None)
        if not symbol:
            continue
        availability = soup.select_one("#availability")
        availability = availability.get_text(" ", strip=True).lower() if availability else ""
        if soup.select_one("#outOfStock") or "unavailable" in availability or "out of stock" in availability:
            in_stock = False
        else:
            in_stock = True if availability else None
        return [{
            "name": title_el.get_text(" ", strip=True),
            "low": amount,
            "high": amount,
            "currency": SYMBOL_TO_CURRENCY[symbol],
            "in_stock": in_stock,
        }]
    return []


def extract_offer(product_name: str, url: str, html: str) -> dict | None:
    """Best matching offer on one page, or None."""
    soup = BeautifulSoup(html, "lxml")
    page_title = soup.title.get_text(strip=True) if soup.title else ""

    if "amazon." in _store_name(url):
        candidates = _offer_from_amazon(soup)
    else:
        candidates = _offers_from_json_ld(soup, page_title) or _offer_from_meta(soup, page_title)

    candidates = [c for c in candidates if matches_product(product_name, c["name"])]
    if not candidates:
        return None

    # Several variants on one page (colours, trims): report the price range.
    domain = _store_name(url)
    return {
        "store": stores.store_name(domain),
        "domain": domain,
        "title": re.sub(r"\s+", " ", candidates[0]["name"]).strip()[:120],
        "price": min(c["low"] for c in candidates),
        "high_price": max(c["high"] for c in candidates),
        "mrp": None,
        "currency": candidates[0]["currency"],
        "in_stock": next((c["in_stock"] for c in candidates if c["in_stock"] is not None), None),
        "url": url,
        "source": "web",
    }


# ── Direct store search ───────────────────────────────────────
def _best_store_offer(product_name: str, listings: list[dict]) -> dict | None:
    """
    The store's offer for this product: the cheapest of its first few
    matching results. Stores rank the exact product first, and colour
    variants of it are often priced differently.
    """
    matches = [l for l in listings if matches_product(product_name, l["title"])]
    matches = matches[: config.STORE_MATCHES_CONSIDERED]
    if not matches:
        return None
    best = min(matches, key=lambda l: l["price"])
    return {
        "store": best["store"],
        "domain": best["domain"],
        "title": best["title"][:120],
        "price": best["price"],
        "high_price": best["price"],
        "mrp": best["mrp"],
        "currency": "INR",
        "in_stock": best["in_stock"],
        "url": best["url"],
        "source": "store",
    }


def _search_stores(product_name: str) -> list[dict]:
    """Search every store in config.PRICE_STORES in parallel."""
    if not config.PRICE_STORES:
        return []
    with ThreadPoolExecutor(max_workers=len(config.PRICE_STORES)) as pool:
        results = pool.map(lambda key: stores.search_store(key, product_name), config.PRICE_STORES)
        offers = [_best_store_offer(product_name, listings) for listings in results]
    return [o for o in offers if o]


def _web_offers(product_name: str) -> list[dict]:
    """Prices from pages found by a web search (brand stores, price trackers, ...)."""
    urls = scraper.search_web(
        f"{product_name} price in {config.PRICE_COUNTRY}",
        num_results=config.PRICE_SEARCH_RESULTS,
        region=config.PRICE_REGION,
    )
    offers = []
    for url, html in zip(urls, scraper.fetch_pages(urls)):
        if not html:
            continue
        try:
            offer = extract_offer(product_name, url, html)
        except Exception as e:  # one odd page must not break the lookup
            log.info("price extraction failed for %s: %s", url, e)
            continue
        if offer:
            offers.append(offer)
    return offers


def _one_per_store(offers: list[dict]) -> list[dict]:
    """
    Keep a single row per store: the direct store search result when there is
    one (it reflects the store's current listing), otherwise the cheapest.
    """
    best: dict[str, dict] = {}
    for o in offers:
        current = best.get(o["domain"])
        if current is None:
            best[o["domain"]] = o
        elif (o["source"], o["price"]) < (current["source"], current["price"]):
            # "store" sorts before "web", then the lower price wins
            best[o["domain"]] = o
    return list(best.values())


# ── Main entry points ─────────────────────────────────────────
def find_prices(product_name: str) -> dict:
    """
    Search stores directly and look for other listings on the web, then
    compare. Returns:
      {
        "offers": [{"store", "domain", "title", "price", "high_price", "mrp",
                    "currency", "in_stock", "url", "source"}, ...],  # cheapest first
        "lowest": offer | None,                 # cheapest listing
        "lowest_available": offer | None,       # cheapest not marked out of stock
        "currency": str | None,
        "error": str | None,
      }
    """
    out = {"offers": [], "lowest": None, "lowest_available": None, "currency": None, "error": None}

    # Store searches run in the background while the web search runs here.
    with ThreadPoolExecutor(max_workers=1) as pool:
        store_future = pool.submit(_search_stores, product_name)
        offers = _web_offers(product_name)
        try:
            offers += store_future.result()
        except Exception as e:
            log.warning("store search failed: %s", e)

    if not offers:
        out["error"] = "No store prices found for this product."
        return out
    offers = _one_per_store(offers)

    # Compare like with like: keep the preferred currency, else the most common one.
    currencies = [o["currency"] for o in offers]
    currency = config.PRICE_CURRENCY if config.PRICE_CURRENCY in currencies else max(
        set(currencies), key=currencies.count
    )
    offers = _drop_price_outliers([o for o in offers if o["currency"] == currency])
    # Cheapest first; on equal prices, list the stores searched directly
    # (Amazon, Flipkart, ...) before price trackers and other sites.
    offers.sort(key=lambda o: (o["price"], o["source"] != "store", o["store"] == o["domain"]))

    out["offers"] = offers[: config.MAX_PRICE_OFFERS]
    out["lowest"] = out["offers"][0]
    # Cheapest listing a buyer can probably act on (not marked out of stock).
    out["lowest_available"] = next((o for o in out["offers"] if o["in_stock"] is not False), None)
    out["currency"] = currency
    return out


def find_prices_async(product_name: str) -> Future:
    """Start a price lookup in the background and return its Future."""
    return _EXECUTOR.submit(find_prices, product_name)


def describe_for_prompt(prices: dict | None) -> str:
    """One-paragraph price summary for the AI prompts."""
    if not prices or not prices.get("offers"):
        return "Prices found online: none. Judge value from what reviewers say about price."
    offers = prices["offers"]
    lowest = prices["lowest"]
    available = prices.get("lowest_available")
    currency = prices["currency"]
    stock = {True: "in stock", False: "out of stock"}.get(lowest["in_stock"], "stock unknown")
    text = (
        f"Prices found online ({currency}): lowest {format_price(lowest['price'], currency)} "
        f"at {lowest['store']} ({stock})"
    )
    if available and available is not lowest and available["url"] != lowest["url"]:
        text += (
            f"; cheapest not out of stock {format_price(available['price'], currency)} "
            f"at {available['store']}"
        )
    if len(offers) > 1:
        high = max(o["high_price"] for o in offers)
        text += (
            f"; {len(offers)} listings ranging from {format_price(lowest['price'], currency)} "
            f"to {format_price(high, currency)}"
        )
    out_of_stock = sum(o["in_stock"] is False for o in offers)
    if out_of_stock:
        text += f" ({out_of_stock} out of stock)"
    return text + "."
