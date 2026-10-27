# local_shops.py
"""
Shops near the user.
- Searches the web for dealers, showrooms and store-locator pages that sell
  the product in the user's area (a locality and city, or a PIN code).
- Reads each page's text, its store listings (schema.org structured data)
  and its Google Maps links.
- Asks the chosen engine which shops on those pages sell the product, then
  checks every shop it names against its page: the name and address must be
  on the page, the address must be in the user's area, and the shop must be
  called an authorised dealer on the page, be listed on the brand's own site,
  or have a Google Maps listing link or its own website. A shop that fails a
  check is dropped, so the engine cannot add a shop, address or phone number
  the pages do not show.
collect_pages() runs without Streamlit so it can be used from a background thread.
"""

import re
import json
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, Future
from urllib.parse import quote_plus, unquote, urlparse

from bs4 import BeautifulSoup

import config
import scraper
from ai_analyzer import request_json

log = logging.getLogger(__name__)

PIN_CODE = re.compile(r"(?<!\d)\d{6}(?!\d)")
PHONE_NUMBER = re.compile(r"\+?\d[\d\s().-]{6,}\d")
MAPS_LINK = re.compile(
    r"google\.[a-z.]+/maps|maps\.google\.|maps\.app\.goo\.gl|goo\.gl/maps|g\.page/", re.IGNORECASE
)
# Map links that open a business listing on Google Maps. Links that only drop
# a pin on coordinates, or search for a name, say nothing about the shop.
MAPS_PLACE = re.compile(
    r"[?&]cid=\d|/maps/place/|place_id|ftid=|maps\.app\.goo\.gl/|goo\.gl/maps/|g\.page/|!1s0x[0-9a-f]+:0x",
    re.IGNORECASE,
)
# Second-level domains under which sites register: example.co.in, example.com.au.
SECOND_LEVEL_DOMAINS = {"co", "com", "net", "org", "gov", "ac", "edu"}
AUTHORISED_WORDS = re.compile(
    r"authori[sz]ed|premium reseller|brand store|"
    r"official (?:store|dealer|partner|reseller|retailer|showroom)|"
    r"exclusive (?:store|dealer|showroom|outlet)",
    re.IGNORECASE,
)

# Words in shop names that do not tell one shop from another.
GENERIC_NAME_WORDS = {
    "the", "and", "of", "in", "at", "store", "stores", "shop", "showroom", "outlet",
    "dealer", "dealers", "mobile", "mobiles", "electronics", "pvt", "ltd", "private",
    "limited", "llp", "india", "authorised", "authorized", "official", "exclusive", "branch",
}
# schema.org types that are not a shop: a brand's head office, the site itself.
NON_SHOP_TYPES = {"Organization", "Corporation", "WebSite", "WebPage", "Person", "Brand", "Product"}
# Other names of Indian cities, so "Bengaluru" also finds "Bangalore".
CITY_ALIASES = [
    {"bengaluru", "bangalore"}, {"mumbai", "bombay"}, {"chennai", "madras"},
    {"kolkata", "calcutta"}, {"gurugram", "gurgaon"}, {"vadodara", "baroda"},
    {"pune", "poona"}, {"thiruvananthapuram", "trivandrum"}, {"kochi", "cochin"},
    {"mysuru", "mysore"}, {"mangaluru", "mangalore"}, {"puducherry", "pondicherry"},
    {"prayagraj", "allahabad"}, {"varanasi", "banaras", "benares"}, {"belagavi", "belgaum"},
    {"hubballi", "hubli"}, {"kozhikode", "calicut"}, {"visakhapatnam", "vizag"},
    {"new delhi", "delhi"},
]
# The postal district of a big city's centre (the first three digits of its
# PIN codes), so shops in the city come before those on the district's outskirts.
CITY_DISTRICTS = {
    "mumbai": "400", "delhi": "110", "bengaluru": "560", "hyderabad": "500", "chennai": "600",
    "kolkata": "700", "pune": "411", "ahmedabad": "380", "jaipur": "302", "lucknow": "226",
    "chandigarh": "160", "kochi": "682", "gurugram": "122", "noida": "201", "nagpur": "440",
    "indore": "452", "bhopal": "462", "patna": "800", "surat": "395", "coimbatore": "641",
    "thiruvananthapuram": "695", "visakhapatnam": "530", "vadodara": "390",
}

# Shop pages are read in the background while reviews are collected.
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="shops")


# ── Text helpers ──────────────────────────────────────────────
def _tokens(text) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").lower())


def _norm(text) -> str:
    return " ".join(_tokens(text))


def _share_found(words: list[str], found: set[str]) -> float:
    """Share of `words` that are in `found`."""
    return sum(w in found for w in words) / len(words) if words else 0.0


def _name_words(name: str) -> list[str]:
    """The words of a shop name that tell it apart: "Croma Store, Koramangala" -> croma, koramangala."""
    words = [w for w in _tokens(name) if w not in GENERIC_NAME_WORDS]
    return words or _tokens(name)


def _phone_digits(text: str) -> set[str]:
    """Phone-like numbers in a text, as their last 10 digits."""
    return {re.sub(r"\D", "", m)[-10:] for m in PHONE_NUMBER.findall(text)}


def _is_place_link(url: str) -> bool:
    url = unquote(url)
    return bool(MAPS_LINK.search(url) and MAPS_PLACE.search(url))


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _site_label(url: str) -> str:
    """The name a site is registered under: dealers-auto.mahindra.com -> "mahindra"."""
    parts = _host(url).split(".")
    if len(parts) >= 3 and parts[-2] in SECOND_LEVEL_DOMAINS and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else parts[0]


def _carries_name(url: str, words: list[str]) -> bool:
    """Whether a site's address carries one of a shop's name words: stores.croma.com for Croma."""
    host = re.sub(r"[.-]", "", _host(url))
    return any(len(w) >= 4 and w in host for w in words)


def _brand_word(product_name: str) -> str | None:
    """The product's brand, taken as the first word of its name: "Mahindra Thar" -> "mahindra"."""
    words = _tokens(product_name)
    return words[0] if words and len(words[0]) >= 3 and not words[0].isdigit() else None


def maps_search_url(query: str) -> str:
    """Google Maps search, for when a shop's own map link was not found."""
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(query)}"


# ── Location ──────────────────────────────────────────────────
def _place_names(part: str) -> set[str]:
    """A place and its other names: "Bengaluru" -> {"bengaluru", "bangalore"}."""
    name = _norm(part)
    return next((group for group in CITY_ALIASES if name in group), {name} if name else set())


def _parse_location(location: str) -> tuple[list[set[str]], list[str]]:
    """
    "Koramangala, Bengaluru 560034" ->
    ([{"koramangala"}, {"bengaluru", "bangalore"}], ["560034"])
    """
    pins = PIN_CODE.findall(location)
    parts = [_place_names(p) for p in re.split(r"[,;/]", PIN_CODE.sub(" ", location))]
    return [p for p in parts if p], pins


def area_match(text: str, location: str) -> int:
    """
    How closely an address (or a page) matches the user's location:
    2 = same locality or PIN code, 1 = same city or postal district, 0 = neither.
    """
    parts, pins = _parse_location(location)
    words = f" {_norm(text)} "
    found_pins = PIN_CODE.findall(text)
    level = 0
    for pin in pins:
        if pin in found_pins:
            return 2
        # The first three digits of a PIN code are its postal district.
        if any(p[:3] == pin[:3] for p in found_pins):
            level = 1
    for i, names in enumerate(parts):
        if any(f" {name} " in words for name in names):
            # The first of several parts is the most specific: "Koramangala, Bengaluru".
            if i == 0 and len(parts) > 1:
                return 2
            level = 1
    return level


# ── Pages ─────────────────────────────────────────────────────
def _is_skipped(url: str) -> bool:
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in config.LOCAL_SKIP_DOMAINS)


def _first_text(value) -> str:
    """A schema.org value as text: the first item of a list, the name of an object."""
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("name") or ""
    # U+FFFD stands for characters a page encoded wrongly.
    return re.sub(r"\s+", " ", str(value or "").replace("�", " ")).strip()


def _address_text(address) -> str:
    """A schema.org PostalAddress (or plain text) as one line."""
    if isinstance(address, list):
        address = address[0] if address else ""
    if isinstance(address, dict):
        keys = ("streetAddress", "addressLocality", "addressRegion", "postalCode")
        return ", ".join(filter(None, (_first_text(address.get(k)) for k in keys)))
    return _first_text(address)


def _urls(value) -> list[str]:
    """Web addresses in a schema.org url / hasMap / sameAs value."""
    values = value if isinstance(value, list) else [value]
    urls = [v.get("url") if isinstance(v, dict) else v for v in values]
    return [u for u in urls if isinstance(u, str) and u.startswith("http")]


def _listings_from_json_ld(soup: BeautifulSoup) -> list[dict]:
    """
    Shops described in schema.org data (LocalBusiness, Store, AutoDealer, ...),
    as {"name", "address", "phone", "url", "map", "text"}.
    """
    found = []

    def walk(data):
        if isinstance(data, dict):
            types = data.get("@type")
            types = {t for t in (types if isinstance(types, list) else [types]) if isinstance(t, str)}
            name = _first_text(data.get("name"))
            address = _address_text(data.get("address"))
            if types and not types <= NON_SHOP_TYPES and name and address:
                phone = _first_text(data.get("telephone"))
                site = [u for u in _urls(data.get("url")) if not MAPS_LINK.search(u)]
                maps = [u for u in _urls(data.get("hasMap")) + _urls(data.get("sameAs")) if _is_place_link(u)]
                found.append({
                    "name": name,
                    "address": address,
                    "phone": phone or None,
                    "url": site[0] if site else None,
                    "map": maps[0] if maps else None,
                    "text": " | ".join(filter(None, [name, address, phone and f"phone {phone}"])),
                })
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


def _map_links(soup: BeautifulSoup) -> list[dict]:
    """
    Links and embedded maps on a page that open a Google Maps business listing,
    each with the text around it (the store card it sits in), so it can be
    matched to a shop.
    """
    links = []
    for el in soup.find_all(["a", "iframe"]):
        url = str(el.get("href") or el.get("src") or "")
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith("http") or not _is_place_link(url):
            continue
        context, block = "", el
        for _ in range(4):  # climb to the first block with some text: the store card
            block = block.parent
            if block is None:
                break
            context = block.get_text(" ", strip=True)
            if len(context) >= 60:
                break
        links.append({"url": url, "context": f"{unquote(url)} {context[:300]}"})
    return links


def read_page(url: str, html: str) -> dict:
    """A page's store listings, Google Maps links and text, from a single parse."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    listings = _listings_from_json_ld(soup)
    map_links = _map_links(soup)
    # Footers are kept: a shop's own site often gives its address there.
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    return {
        "url": url,
        "domain": _host(url),
        "title": title[:150],
        "listings": listings,
        "map_links": map_links,
        "text": re.sub(r"\s+", " ", soup.get_text(" ", strip=True)),
    }


def _page_text(page: dict) -> str:
    """Page text plus its store listings: everything a shop's details are checked against."""
    return " ".join([page["text"]] + [l["text"] for l in page["listings"]])


def collect_pages(product_name: str, location: str) -> list[dict]:
    """
    Search the web for shops selling the product in the user's area and read
    the pages found. Pages that never mention the area are left out.
    """
    urls = []
    for query in (f"{product_name} authorised dealer in {location}",
                  f"{product_name} store near {location} address"):
        for url in scraper.search_web(query, num_results=config.LOCAL_SEARCH_RESULTS):
            if url not in urls and not _is_skipped(url):
                urls.append(url)

    pages = []
    for url, html in zip(urls, scraper.fetch_pages(urls)):
        if not html:
            continue
        try:
            page = read_page(url, html)
        except Exception as e:  # one odd page must not break the search
            log.info("shop page extraction failed for %s: %s", url, e)
            continue
        if area_match(_page_text(page), location):
            pages.append(page)
    return pages


def collect_pages_async(product_name: str, location: str) -> Future:
    """Start collect_pages() in the background and return its Future."""
    return _EXECUTOR.submit(collect_pages, product_name, location)


# ── Engine ────────────────────────────────────────────────────
def _excerpt(text: str, location: str, limit: int) -> str:
    """For a long page, the parts around mentions of the user's area and PIN codes."""
    if len(text) <= limit:
        return text
    parts, _ = _parse_location(location)
    hits = [m.start() for m in PIN_CODE.finditer(text)]
    for names in parts:
        for name in names:
            hits += [m.start() for m in re.finditer(re.escape(name), text, re.IGNORECASE)]

    windows = []
    for hit in sorted(hits):
        start, end = max(hit - 300, 0), hit + 300
        if windows and start <= windows[-1][1]:
            windows[-1][1] = end
        else:
            windows.append([start, end])

    pieces, total = [], 0
    for start, end in windows:
        piece = text[start:end][: limit - total]
        pieces.append(piece)
        total += len(piece)
        if total >= limit:
            break
    return " ... ".join(pieces) or text[:limit]


def _build_prompt(product_name: str, location: str, pages: list[dict]) -> str:
    blocks = [f"Product: {product_name}\nBuyer's location: {location}"]
    for i, page in enumerate(pages, 1):
        lines = [f"[P{i}] {page['domain']}: {page['title']}"]
        nearby = [l for l in page["listings"] if area_match(l["address"], location)]
        lines += [f"Store listing: {l['text']}" for l in nearby[: config.LOCAL_LISTINGS_PER_PAGE]]
        lines.append("Text: " + _excerpt(page["text"], location, config.LOCAL_CHARS_PER_PAGE))
        blocks.append("\n".join(lines))
    blocks.append("List the shops near the buyer where this product can be bought. Return JSON.")
    return "\n\n".join(blocks)


# ── Checks ────────────────────────────────────────────────────
def _page_index(value, count: int) -> int | None:
    """2, "2" or "P2" -> 1 (0-based), or None if it is not one of the pages."""
    try:
        index = int(str(value).strip().lstrip("Pp[").rstrip("]")) - 1
    except ValueError:
        return None
    return index if 0 <= index < count else None


def _shop_map_link(page: dict, name_words: list[str], address: str) -> str | None:
    """
    The page's Google Maps link for this shop: the one in the block that
    names the shop. If several do, the shop's PIN code must be there too.
    """
    near = {m["url"]: m["context"] for m in page["map_links"]
            if _share_found(name_words, set(_tokens(m["context"]))) >= 0.6}
    pins = PIN_CODE.findall(address)
    if len(near) > 1 and pins:
        near = {url: context for url, context in near.items() if pins[0] in context}
    return next(iter(near)) if len(near) == 1 else None


def _check_shop(item, pages: list[dict], location: str, brand: str | None) -> dict | None:
    """
    A shop the engine named, checked against the page it says the shop is on.
    Returns the shop with what could be verified about it, or None if its
    name or address is not on that page or the address is not in the user's area.
    """
    if not isinstance(item, dict):
        return None
    index = _page_index(item.get("page"), len(pages))
    name = _first_text(item.get("name"))
    address = _first_text(item.get("address"))
    if index is None or not name or not address:
        return None
    page = pages[index]
    text = _page_text(page)
    words = set(_tokens(text))

    # The name and address must be written on the page, PIN code included.
    if _share_found(_tokens(name), words) < 0.75 or _share_found(_tokens(address), words) < 0.7:
        return None
    if any(pin not in words for pin in PIN_CODE.findall(address)):
        return None
    area = area_match(address, location)
    if not area:
        return None

    phone = _first_text(item.get("phone"))
    if re.sub(r"\D", "", phone)[-10:] not in _phone_digits(text):
        phone = None

    # Authorised only if the page says so in words the check can find.
    quote = _first_text(item.get("authorised_quote"))
    authorised = (
        str(item.get("authorised")).lower() == "true"
        and AUTHORISED_WORDS.search(quote) is not None
        and f" {_norm(quote)} " in f" {_norm(text)} "
    )

    # A brand's own dealer locator lists only its dealers: dealers-auto.mahindra.com
    # for a Mahindra. The site must be registered under the brand's exact name.
    brand_listed = brand is not None and _site_label(page["url"]) in (brand, brand + "india")

    # The shop's own entry in the page's structured data, if it has one.
    name_words = _name_words(name)
    listing = next(
        (l for l in page["listings"]
         if _share_found(name_words, set(_tokens(l["name"]))) >= 0.6
         and _share_found(_tokens(address), set(_tokens(l["address"]))) >= 0.6),
        {},
    )
    # Its website: the page itself when the site carries the shop's name
    # (stores.croma.com for a Croma store), or a link in its structured data
    # to another site. A directory's own page about the shop does not count.
    own_words = [w for w in name_words if w != brand]
    own_site = _carries_name(page["url"], own_words)
    website = listing.get("url")
    if website and not _carries_name(website, own_words) and _host(website) == page["domain"]:
        website = None
    website = website or (page["url"] if own_site else None)
    if website and _is_skipped(website):
        website = None
    maps_url = listing.get("map") or _shop_map_link(page, name_words, address)
    # A shop's own page with a single map shows that shop.
    if not maps_url and own_site and len({m["url"] for m in page["map_links"]}) == 1:
        maps_url = page["map_links"][0]["url"]

    return {
        "name": name,
        "address": address,
        "phone": phone,
        "authorised": authorised,
        "authorised_quote": quote if authorised else None,
        "brand_listed": brand_listed,
        "maps_url": maps_url,
        "website": website,
        "source": page["url"],
        "area": area,
    }


def checks(shop: dict) -> list[str]:
    """What was verified about a shop, strongest first. Shops with none are not shown."""
    found = []
    if shop["authorised"]:
        found.append("Authorised dealer")
    if shop["brand_listed"]:
        found.append("Listed on brand site")
    if shop["maps_url"]:
        found.append("On Google Maps")
    if shop["website"]:
        found.append("Website")
    return found


def _district(shop: dict) -> str | None:
    """The postal district of a shop's address: the first three digits of its PIN code."""
    pins = PIN_CODE.findall(shop["address"])
    return pins[0][:3] if pins else None


def _core_district(location: str, shops: list[dict]) -> str | None:
    """
    The postal district the user means: that of their PIN code, of the big city
    they named, or else the one most of the shops found share.
    """
    parts, pins = _parse_location(location)
    if pins:
        return pins[0][:3]
    for names in parts:
        for name in names:
            if name in CITY_DISTRICTS:
                return CITY_DISTRICTS[name]
    districts = Counter(d for d in map(_district, shops) if d)
    return districts.most_common(1)[0][0] if districts else None


def _rank(shops: list[dict], location: str) -> list[dict]:
    """
    Authorised dealers first, then the closest, then the best verified.
    A city name alone makes every shop in its district "your city", so shops in
    the city's own postal district (411 for Pune) come before those on the
    district's outskirts (410 for Narayangaon).
    """
    core = _core_district(location, shops)
    return sorted(
        shops,
        key=lambda s: (
            not (s["authorised"] or s["brand_listed"]),
            -s["area"],
            _district(s) != core,
            -len(checks(s)),
            s["phone"] is None,
        ),
    )


# ── Main entry point ──────────────────────────────────────────
def find_shops(product_name: str, location: str, pages: list[dict] | None,
               engine: str = config.DEFAULT_ENGINE) -> dict:
    """
    Pick the verified shops near `location` from the pages collect_pages() found
    (None if that search did not finish).
    Returns:
      {
        "location": str,
        "shops": [{"name", "address", "phone", "authorised", "authorised_quote",
                   "brand_listed", "maps_url", "website", "source", "area"}, ...],  # best first
        "pages": int,                 # pages the shops were picked from
        "maps_search_url": str,       # Google Maps search for the product in the area
        "error": str | None,
      }
    """
    out = {
        "location": location,
        "shops": [],
        "pages": 0,
        "maps_search_url": maps_search_url(f"{product_name} store near {location}"),
        "error": None,
    }
    if pages is None:
        out["error"] = "The shop search did not finish. Try again later."
        return out
    pages = pages[: config.LOCAL_PAGES]
    out["pages"] = len(pages)
    if not pages:
        out["error"] = f"No shop pages mentioning {location} were found."
        return out

    data, error = request_json(
        config.LOCAL_SHOPS_SYSTEM_PROMPT,
        _build_prompt(product_name, location, pages),
        engine,
        required=("shops",),
    )
    if error:
        out["error"] = error
        return out

    brand = _brand_word(product_name)
    items = data.get("shops")
    best: dict[tuple, dict] = {}
    for item in items if isinstance(items, list) else []:
        shop = _check_shop(item, pages, location, brand)
        if not shop or not checks(shop):
            continue
        # The same shop found on two pages: keep everything verified about it.
        pins = PIN_CODE.findall(shop["address"])
        key = (" ".join(sorted(_name_words(shop["name"]))), pins[0] if pins else _norm(shop["address"])[:40])
        kept = best.setdefault(key, shop)
        if kept is not shop:
            for field in ("phone", "maps_url", "website"):
                kept[field] = kept[field] or shop[field]
            kept["brand_listed"] = kept["brand_listed"] or shop["brand_listed"]
            if shop["authorised"] and not kept["authorised"]:
                kept["authorised"], kept["authorised_quote"] = True, shop["authorised_quote"]

    out["shops"] = _rank(list(best.values()), location)[: config.MAX_LOCAL_SHOPS]
    if not out["shops"]:
        out["error"] = f"No shop near {location} could be verified on the pages found."
    return out
