# app.py
"""
ReviewLens: product review analyzer (Streamlit UI).

Flow:
  0. Sign in with Google (allowed emails only); each user has a daily
     credit limit (access.py).
  1. Collect reviews: scrape the web (scraper.py) or parse pasted text.
  2. Analyze them with the chosen engine: pros, cons, sentiment, verdict (ai_analyzer.py).
  3. Suggest related products (related_products.py).
  4. Show the result, keep it in session history, offer downloads (report.py).

The UI never names the model provider: engines are shown as "Engine 1" and
"Engine 2" (config.ENGINES).

Run with:  streamlit run app.py
"""

import re
import logging
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse

import streamlit as st

import config
import access
import scraper
import prices
import ai_analyzer
import related_products
import report

logging.basicConfig(level=logging.WARNING, format="%(levelname)s [%(name)s] %(message)s")

SOURCE_WEB = "web"
SOURCE_PASTE = "paste"
SOURCE_LABELS = {
    SOURCE_WEB: ":material/travel_explore: Search the web",
    SOURCE_PASTE: ":material/content_paste: Paste reviews",
}
SENTIMENT_COLORS = {"Positive": "green", "Mixed": "orange", "Negative": "red"}
# Bar segments for the sentiment breakdown; each is also labelled in text.
SENTIMENT_BAR_COLORS = {"Positive": "#16A34A", "Mixed": "#F59E0B", "Negative": "#DC2626"}
VALUE_COLORS = {"Excellent": "green", "Good": "blue", "Fair": "orange", "Poor": "red"}
CONFIDENCE_COLORS = {"High": "green", "Medium": "blue", "Low": "orange"}
STOCK_LABELS = {True: "In stock", False: "Out of stock", None: "Check store"}
REVIEW_PREVIEW_CHARS = 400

# ── Page config (must be the first Streamlit command) ─────────
st.set_page_config(
    page_title=config.PAGE_TITLE,
    page_icon=config.PAGE_ICON,
    layout=config.LAYOUT,
    initial_sidebar_state="expanded",
)

# ── Initialize session state ──────────────────────────────────
config.init_session()


# ── Helpers ───────────────────────────────────────────────────
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]<>#+|~$])")


def md(text) -> str:
    """Escape markdown in scraped / AI text ("$699" would otherwise render as LaTeX)."""
    return _MD_SPECIAL.sub(r"\\\1", str(text))


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def site_name(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def page_name(url: str, limit: int = 70) -> str:
    """Site and path, so two pages from one site can be told apart."""
    parts = urlparse(url)
    text = site_name(url) + parts.path.rstrip("/")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def get_current_result():
    history = st.session_state[config.RESULTS_KEY]
    index = st.session_state[config.CURRENT_KEY]
    if index is None or not 0 <= index < len(history):
        return None
    return history[index]


def select_result(index):
    st.session_state[config.CURRENT_KEY] = index


def find_result(product_name):
    """Index of an earlier analysis of this product, so re-opening it is free."""
    target = product_name.strip().lower()
    for i, item in enumerate(st.session_state[config.RESULTS_KEY]):
        names = (item["product"].strip().lower(), item["analysis"]["product_name"].strip().lower())
        if target in names:
            return i
    return None


def switch_to_paste():
    st.session_state["source_mode"] = SOURCE_PASTE


def clear_history():
    st.session_state[config.RESULTS_KEY] = []
    st.session_state[config.CURRENT_KEY] = None


def current_user():
    """The signed-in user's email, which is also their key for daily credits."""
    return str(st.user.get("email") or "").strip().lower()


def engine_label(engine):
    return config.ENGINES[engine]["label"] if engine in config.ENGINES else None


def can_start_analysis():
    """Whether the chosen engine is set up and the user has a credit left today."""
    engine = st.session_state.get(config.ENGINE_KEY, config.DEFAULT_ENGINE)
    return bool(ai_analyzer.get_api_key(engine)) and access.remaining(current_user()) > 0


# ── Analysis pipeline ─────────────────────────────────────────
def run_analysis(product_name, source, manual_text, max_reviews, include_related, engine,
                 related_to=None, offer_paste=True):
    """
    Collect reviews, analyze them with `engine` and store the result in history.
    A credit is taken when the work starts, so two tabs cannot both use the
    last one, and given back if the analysis fails.
    `related_to` is the history index of the product this one was suggested for.
    Returns the new history index, or None on failure.
    """
    if len(product_name) < 2:
        st.warning("Enter a product name, ideally with brand and model.")
        return None

    if source == SOURCE_PASTE:
        reviews = scraper.parse_manual_reviews(manual_text)
        if not reviews:
            st.warning(
                "No usable reviews found in the pasted text. Separate reviews with a "
                f"blank line or number them, and make each at least "
                f"{config.MIN_CHARS_PER_REVIEW} characters long."
            )
            return None
        scraped = {
            "product_name": product_name,
            "reviews": reviews[:max_reviews],
            "review_sources": [],
            "user_rating": None,
            "rating_pages": 0,
            "sources": [],
            "error": None,
        }

    user = current_user()
    credit_day = access.reserve(user)
    if credit_day is None:
        st.error(
            f"You have used all {config.DAILY_LIMIT_PER_USER} credits for today. "
            f"They reset at midnight {config.LIMIT_TIMEZONE_NAME}."
        )
        return None

    # Store prices are looked up in the background while reviews are collected.
    price_future = prices.find_prices_async(product_name)

    failure = None
    try:
        with st.status(f"Analyzing {product_name}", expanded=True) as status:
            if source == SOURCE_WEB:
                scraped = scraper.scrape_reviews(product_name, max_reviews=max_reviews, progress=st.write)
                if scraped["error"]:
                    failure = scraped["error"]
                    status.update(label="Could not collect reviews", state="error")
                else:
                    st.write(
                        f"Collected {plural(len(scraped['reviews']), 'snippet')} "
                        f"from {plural(len(scraped['sources']), 'page')}"
                    )

            if not failure:
                st.write("Comparing store prices")
                try:
                    price_data = price_future.result(timeout=90)
                except Exception as e:  # prices are a bonus; never fail the analysis
                    price_data = {"offers": [], "lowest": None, "lowest_available": None,
                                  "currency": None, "error": str(e)}
                if price_data["lowest"]:
                    lowest = price_data["lowest"]
                    st.write(
                        f"Lowest price found: {md(prices.format_price(lowest['price'], lowest['currency']))} "
                        f"at {lowest['store']}"
                    )
                else:
                    st.write("No store prices found")

                st.write("Reading the reviews")
                analysis = ai_analyzer.analyze_reviews(
                    product_name,
                    scraped["reviews"],
                    scraped["user_rating"],
                    prices=price_data,
                    review_sources=scraped.get("review_sources") or None,
                    engine=engine,
                )
                if "error" in analysis:
                    failure = analysis["error"]
                    status.update(label="Analysis failed", state="error")
                else:
                    st.write(
                        f"{plural(analysis['total_reviews_analyzed'], 'genuine review')} "
                        f"found among {analysis['reviews_sent']} snippets"
                    )

            if not failure:
                related = None
                if include_related:
                    st.write("Finding alternatives")
                    related = related_products.get_related_products(
                        analysis["product_name"], analysis, prices=price_data, engine=engine
                    )
                status.update(label="Analysis complete", state="complete", expanded=False)
    except Exception:
        # A crash is a failed attempt too. A run stopped part-way by a page
        # interaction or reload is not an Exception and keeps the credit,
        # since the engine may already have done the work.
        access.refund(user, credit_day)
        raise

    if failure:
        access.refund(user, credit_day)
        st.error(failure + " No credit was used.", icon=":material/error:")
        if source == SOURCE_WEB and offer_paste:
            st.button("Paste reviews instead", on_click=switch_to_paste, icon=":material/content_paste:")
        return None

    history = st.session_state[config.RESULTS_KEY]
    history.append(
        {
            "product": product_name,
            "source": "web" if source == SOURCE_WEB else "manual",
            "engine": engine,
            "created_at": datetime.now().strftime("%d %b %Y, %H:%M"),
            "scraped": scraped,
            "prices": price_data,
            "analysis": analysis,
            "related": related,
            "related_to": related_to,
        }
    )
    return len(history) - 1


# ── Result view ───────────────────────────────────────────────
def render_result(result, compact=False):
    """
    Draw an analysis. `compact` (used inside the related-product dialog) hides
    export and related products, since dialogs cannot open other dialogs.
    Returns the name of a related product whose "Analyze reviews" button was
    clicked, or None.
    """
    a = result["analysis"]
    history = st.session_state[config.RESULTS_KEY]

    # Link back to the product this one was suggested for
    parent = result.get("related_to")
    if not compact and parent is not None and 0 <= parent < len(history):
        st.button(
            f"Back to {history[parent]['analysis']['product_name']}",
            type="tertiary",
            icon=":material/arrow_back:",
            on_click=select_result,
            args=(parent,),
        )

    render_header(result, compact)
    render_key_numbers(result)
    render_verdict(a)
    render_pros_cons(a)
    render_prices(result)

    clicked = None
    related = result.get("related")
    if related and not compact:
        clicked = render_related(related, result)

    render_evidence(result)
    return clicked


def render_header(result, compact):
    a = result["analysis"]
    web = result["source"] == "web"
    sentiment = a["overall_sentiment"]
    confidence = a["confidence"]
    # Older results in the session predate the engine choice.
    engine = engine_label(result.get("engine"))

    head, export = st.columns([4, 1], vertical_alignment="bottom")
    with head:
        st.header(md(a["product_name"]), anchor=False)
        st.markdown(
            f":{SENTIMENT_COLORS.get(sentiment, 'gray')}-badge[{sentiment}] "
            f":{CONFIDENCE_COLORS.get(confidence, 'gray')}-badge[{confidence} confidence] "
            f":gray-badge[{'Web reviews' if web else 'Pasted reviews'}]"
            + (f" :gray-badge[{engine}]" if engine else "")
        )
        reviews = plural(a["total_reviews_analyzed"], "review")
        sites = a.get("source_count")
        basis = f"Based on {reviews} from {plural(sites, 'site')}" if web and sites else f"Based on {reviews}"
        st.caption(f"{basis}  |  {result['created_at']}")
    if not compact:
        render_export(export, result)


def render_key_numbers(result):
    a = result["analysis"]
    price_data = result.get("prices") or {}
    lowest = price_data.get("lowest")
    # Older results in the session predate "lowest_available".
    buy = price_data.get("lowest_available", lowest)
    best = buy or lowest

    score = a["sentiment_score"]
    rating = a["average_user_rating"]
    pages = result["scraped"].get("rating_pages") or 0
    if a["rating_source"] == "scraped":
        rating_help = (
            f"Median of the ratings published on {plural(pages, 'review page')}."
            if pages else "Rating published on the review pages."
        )
    elif a["rating_source"] == "ai_estimate":
        rating_help = "No published rating was found, so this is estimated from what reviewers wrote."
    else:
        rating_help = "No rating could be determined."

    if buy:
        price_help = f"At {buy['store']}. The cheapest listing not marked out of stock."
    elif lowest:
        price_help = f"At {lowest['store']}. Marked out of stock."
    else:
        price_help = "No store prices could be read for this product."

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Sentiment score",
        f"{round(score * 100)} / 100" if score is not None else "N/A",
        help=(
            "Share of reviews that are positive, with mixed reviews counting half. "
            "0 means all negative, 100 means all positive."
        ),
        border=True,
    )
    c2.metric(
        "User rating" if a["rating_source"] == "scraped" else "Estimated rating",
        f"{rating} / 5" if rating is not None else "N/A",
        help=rating_help,
        border=True,
    )
    c3.metric(
        "Best price",
        prices.format_price(best["price"], best["currency"]) if best else "N/A",
        help=price_help,
        border=True,
    )
    c4.metric(
        "Value for money",
        a.get("value_for_money", "Unknown"),
        help=a.get("value_reason") or None,
        border=True,
    )


def render_verdict(a):
    with st.container(border=True):
        st.markdown("##### Verdict")
        st.markdown(md(a["buying_verdict"]))
        if a["summary"]:
            st.caption(md(a["summary"]))
        counts = a.get("sentiment_counts")
        if counts and sum(counts.values()):
            render_sentiment_breakdown(counts)


def render_sentiment_breakdown(counts):
    """A thin stacked bar of positive / mixed / negative reviews, with a text legend."""
    segments = "".join(
        f'<div style="flex:{n};background:{SENTIMENT_BAR_COLORS[s]}"></div>'
        for s, n in counts.items() if n
    )
    summary = ", ".join(f"{n} {s.lower()}" for s, n in counts.items())
    st.markdown(
        f'<div role="img" aria-label="Review sentiment: {summary}" style="display:flex;'
        f'gap:3px;height:8px;border-radius:4px;overflow:hidden;margin:0.5rem 0 0.25rem">'
        f"{segments}</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "  |  ".join(
            f":{SENTIMENT_COLORS[s]}[**{n}** {s.lower()}]" for s, n in counts.items()
        )
    )


def _points_md(texts, mentions):
    """Bullet list of pros or cons with how many reviews mention each."""
    mentions = mentions or [0] * len(texts)
    return "\n".join(
        f"- {md(text)}" + (f" :gray[({plural(n, 'review')})]" if n else "")
        for text, n in zip(texts, mentions)
    )


def render_pros_cons(a):
    col_pros, col_cons = st.columns(2)
    with col_pros.container(border=True, height="stretch"):
        st.markdown("##### :green[:material/thumb_up:] What buyers like")
        st.markdown(_points_md(a["pros"], a.get("pro_mentions")) or "No clear pros found.")
    with col_cons.container(border=True, height="stretch"):
        st.markdown("##### :red[:material/thumb_down:] Common complaints")
        st.markdown(_points_md(a["cons"], a.get("con_mentions")) or "No clear complaints found.")


def render_export(container, result):
    slug = report.slugify(result["analysis"]["product_name"])
    with container.popover("Export", icon=":material/download:", width="stretch"):
        st.download_button(
            "Report (Markdown)",
            data=report.to_markdown(result),
            file_name=f"{slug}-review-analysis.md",
            mime="text/markdown",
            icon=":material/description:",
            width="stretch",
        )
        st.download_button(
            "Raw data (JSON)",
            data=report.to_json(result),
            file_name=f"{slug}-review-analysis.json",
            mime="application/json",
            icon=":material/data_object:",
            width="stretch",
        )


def _price_text(offer):
    """'1,299' or '799 - 868' when a page lists several variants."""
    text = prices.format_price(offer["price"], offer["currency"])
    if offer["high_price"] > offer["price"] * 1.01:
        text += f" - {prices.format_price(offer['high_price'], offer['currency'])}"
    return text


def render_prices(result):
    """Where to buy: value for money, the best listing to act on, and every listing found."""
    a = result["analysis"]
    price_data = result.get("prices") or {}
    offers = price_data.get("offers") or []
    lowest = price_data.get("lowest")
    buy = price_data.get("lowest_available", lowest)
    value = a.get("value_for_money", "Unknown")

    st.subheader("Where to buy", anchor=False)
    with st.container(border=True):
        info, action = st.columns([3, 1], vertical_alignment="center")
        with info:
            st.markdown(f"**Value for money** :{VALUE_COLORS.get(value, 'gray')}-badge[{value}]")
            if a.get("value_reason"):
                st.markdown(md(a["value_reason"]))
            notes = []
            if not offers:
                notes.append("No store prices could be read for this product.")
            if lowest and buy and buy["url"] != lowest["url"]:
                notes.append(
                    f"The cheapest listing, {prices.format_price(lowest['price'], lowest['currency'])} "
                    f"at {lowest['store']}, is marked out of stock."
                )
            available = [o["price"] for o in offers if o["in_stock"] is not False]
            if buy and available and max(available) > buy["price"] * 1.01:
                saving = prices.format_price(max(available) - buy["price"], buy["currency"])
                notes.append(
                    f"Buying at {buy['store']} saves up to {saving} compared with "
                    "the most expensive listing found."
                )
            if notes:
                st.caption(md(" ".join(notes)))
        with action:
            target = buy or lowest
            if target:
                st.link_button(
                    f"Buy at {target['store']}" if buy else "View cheapest listing",
                    target["url"],
                    type="primary",
                    icon=":material/shopping_cart:",
                    width="stretch",
                )
            else:
                st.link_button(
                    "Search prices",
                    prices.shopping_search_url(a["product_name"]),
                    icon=":material/open_in_new:",
                    width="stretch",
                )

    if offers:
        st.caption(
            f"{plural(len(offers), 'listing')} compared across "
            f"{plural(len({o['store'] for o in offers}), 'store')} and price sites, cheapest first."
        )
        rows = []
        for i, o in enumerate(offers):
            diff = o["price"] - lowest["price"]
            if i == 0:
                compare = "Best price"
            elif abs(diff) < 1:
                compare = "Same price"
            else:
                sign = "+" if diff > 0 else "-"
                compare = f"{sign}{prices.format_price(abs(diff), o['currency'])}"
            mrp = o.get("mrp")
            rows.append({
                "Store": o["store"],
                "Price": _price_text(o),
                "MRP": prices.format_price(mrp, o["currency"]) if mrp else "",
                "Off": f"{round((mrp - o['price']) / mrp * 100)}%" if mrp else "",
                "Compared": compare,
                "Stock": STOCK_LABELS[o["in_stock"]],
                "Listing": o["title"],
                "Link": o["url"],
            })
        st.dataframe(
            rows,
            hide_index=True,
            width="stretch",
            column_config={
                "Off": st.column_config.TextColumn("Off", help="Discount from MRP, where the store shows one."),
                "Compared": st.column_config.TextColumn("Compared", help="Difference from the lowest price."),
                "Listing": st.column_config.TextColumn("Listing", width="large"),
                "Link": st.column_config.LinkColumn("Link", display_text="Open"),
            },
        )
    st.caption(
        "Prices are read from public store and price-comparison pages. They may be out "
        "of date, exclude delivery, or belong to a different variant. Check the store "
        "before buying."
    )


def _related_rating_caption(p):
    """The real rating when the product has already been analyzed, otherwise the estimate."""
    index = find_result(p["name"])
    if index is not None:
        known = st.session_state[config.RESULTS_KEY][index]["analysis"]
        if known["average_user_rating"] is not None:
            kind = "published" if known["rating_source"] == "scraped" else "from reviews"
            return f"Rating: {known['average_user_rating']} / 5 ({kind})"
    if p["ai_suggested_rating"]:
        return f"Rating: about {p['ai_suggested_rating']} / 5 (estimate)"
    return "Rating: not known"


def _related_price_caption(p, result):
    """
    Price line for a related-product card. Uses the real lowest price when the
    product has already been analyzed, otherwise the estimate, and compares
    it with the main product's lowest price.
    """
    index = find_result(p["name"])
    known = (st.session_state[config.RESULTS_KEY][index].get("prices") or {}).get("lowest") if index is not None else None
    if known:
        price, currency, label = known["price"], known["currency"], "lowest found"
    elif p.get("approx_price"):
        price, currency, label = p["approx_price"], p.get("currency", config.PRICE_CURRENCY), "estimate"
    else:
        return "Price: not known"

    # md() escapes "$" so USD prices are not rendered as LaTeX.
    text = f"Price: {md(prices.format_price(price, currency))} ({label})"
    main = (result.get("prices") or {}).get("lowest")
    if main and main["currency"] == currency:
        diff = price - main["price"]
        if abs(diff) <= 0.05 * main["price"]:
            text += "  \n:gray[About the same price]"
        elif diff < 0:
            text += f"  \n:green[About {md(prices.format_price(-diff, currency))} cheaper]"
        else:
            text += f"  \n:orange[About {md(prices.format_price(diff, currency))} more]"
    return text


def render_related(related, result):
    """Alternative-product cards. Returns the name of the product clicked, or None."""
    st.subheader("Alternatives to compare", anchor=False)
    if related["error"]:
        st.info(f"Alternatives are unavailable: {related['error']}")
        return None

    st.caption(
        "Suggested alternatives. Their ratings and prices are estimates until you "
        "analyze them, which collects their real reviews and store prices."
    )
    can_analyze = can_start_analysis()
    clicked = None
    items = related["related_products"]
    for i, (col, p) in enumerate(zip(st.columns(len(items)), items)):
        with col.container(border=True, height="stretch"):
            st.markdown(f"**{md(p['name'])}**")
            st.caption(_related_rating_caption(p))
            st.markdown(_related_price_caption(p, result))
            if p["why_similar"]:
                st.markdown(md(p["why_similar"]))

            analyzed = find_result(p["name"]) is not None
            if st.button(
                "Open analysis" if analyzed else "Analyze reviews",
                key=f"view_related_{i}",
                type="primary",
                icon=":material/reviews:",
                width="stretch",
                disabled=not (analyzed or can_analyze),
                help=(
                    "Already analyzed. Opens without using a credit." if analyzed
                    else "Collects and analyzes this product's reviews and prices. Uses 1 credit."
                ),
            ):
                clicked = p["name"]
            st.link_button(
                "Search the web",
                p["search_url"],
                type="tertiary",
                icon=":material/open_in_new:",
                width="stretch",
            )
    return clicked


def render_evidence(result):
    """The reviews behind the analysis, which were set aside, and where they came from."""
    a = result["analysis"]
    scraped = result["scraped"]
    reviews = scraped["reviews"]
    sources = scraped.get("review_sources") or []
    # Older results in the session have no per-review classification: treat all as used.
    labels = a.get("review_sentiments") or [None] * len(reviews)
    classified = bool(a.get("review_sentiments"))

    used = [i for i in range(len(reviews)) if not classified or (i < len(labels) and labels[i])]
    unused = sorted(set(range(len(reviews))) - set(used))

    names = [f"Reviews used ({len(used)})"]
    if unused:
        names.append(f"Set aside ({len(unused)})")
    names.append(f"Sources ({len(scraped['sources'])})")
    tabs = st.tabs(names)

    with tabs[0]:
        _render_review_list(used, reviews, labels, sources)
    if unused:
        with tabs[1]:
            st.caption(
                "Text collected from the pages that was not an opinion about this product, "
                "such as specs, menus or other models. It did not affect the result."
            )
            _render_review_list(unused, reviews, labels, sources)
    with tabs[-1]:
        if scraped["sources"]:
            per_site = Counter(sources[i] for i in used if i < len(sources))
            for url in scraped["sources"]:
                n = per_site.get(url)
                count = f" :gray[({plural(n, 'review')} used)]" if n else ""
                st.markdown(f"- [{md(page_name(url))}](<{url}>){count}")
        else:
            st.write("Reviews were pasted manually.")


def _render_review_list(indexes, reviews, labels, sources):
    with st.container(height=380, border=False):
        for i in indexes:
            text = reviews[i]
            label = labels[i] if i < len(labels) else None
            badge = f":{SENTIMENT_COLORS[label]}-badge[{label}] " if label else ""
            more = "..." if len(text) > REVIEW_PREVIEW_CHARS else ""
            where = f"  \n:gray[{md(site_name(sources[i]))}]" if i < len(sources) else ""
            st.markdown(f"{badge}{md(text[:REVIEW_PREVIEW_CHARS])}{more}{where}")


@st.dialog("Product reviews", width="large", on_dismiss="rerun")
def related_reviews_dialog(product_name, parent_index, max_reviews, include_related, engine):
    """
    Analyze a related product without leaving the current page.
    Closing the dialog reruns the app so the credit counter and history update.
    """
    index = find_result(product_name)
    if index is None:
        index = run_analysis(
            product_name, SOURCE_WEB, "", max_reviews, include_related, engine,
            related_to=parent_index, offer_paste=False,
        )
        if index is None:
            return

    render_result(st.session_state[config.RESULTS_KEY][index], compact=True)
    if st.button("Open full analysis", type="primary", icon=":material/open_in_full:", width="stretch"):
        select_result(index)
        st.rerun()


def render_empty_state():
    st.subheader("How it works", anchor=False)
    steps = [
        (":material/travel_explore:", "Collect",
         "Finds review pages for the product and pulls out what buyers wrote, or uses reviews you paste."),
        (":material/fact_check:", "Analyze",
         "Separates genuine opinions from page clutter, counts positive and negative ones, "
         "and lists the pros and complaints mentioned most."),
        (":material/storefront:", "Compare",
         "Checks prices at major stores and suggests alternatives you can analyze in one click."),
    ]
    for col, (icon, title, text) in zip(st.columns(3), steps):
        with col.container(border=True, height="stretch"):
            st.markdown(f"**:primary[{icon}] {title}**")
            st.caption(text)


def render_gate_header():
    """App name above the sign-in and no-access cards."""
    st.title(config.APP_NAME, anchor=False)
    st.caption(
        "Check a product before you buy: what buyers like, what they complain "
        "about, a buying verdict and the best store price."
    )


def render_sign_in(problem):
    """Sign-in page, shown until the user signs in with Google."""
    _, middle, _ = st.columns([1, 2, 1])
    with middle:
        render_gate_header()
        if problem:
            st.error(problem, icon=":material/lock:")
            return
        with st.container(border=True):
            st.markdown("##### Sign in")
            st.markdown("Use your Google account. No new password needed.")
            if st.button("Sign in with Google", type="primary", icon=":material/login:", width="stretch"):
                st.login()
            st.caption(
                f"You stay signed in on this browser until you sign out. Each account "
                f"gets {config.DAILY_LIMIT_PER_USER} analyses a day."
            )


def render_no_access():
    """Shown to a signed-in Google account that is not on the allow list."""
    email = current_user()
    _, middle, _ = st.columns([1, 2, 1])
    with middle:
        render_gate_header()
        with st.container(border=True):
            st.markdown("##### No access yet")
            if email:
                st.markdown(
                    f"**{md(email)}** is not on the list of accounts that can use "
                    f"{config.APP_NAME}. Ask the owner to add it, or use another account."
                )
            else:
                st.markdown(
                    "Your Google account did not share an email address, so access "
                    "cannot be checked. Try another account."
                )
            if st.button("Use another account", icon=":material/switch_account:", width="stretch"):
                st.logout()


# ── Sign-in ───────────────────────────────────────────────────
sign_in_problem = access.sign_in_problem()
if sign_in_problem or not st.user.is_logged_in:
    render_sign_in(sign_in_problem)
    st.stop()
# Google marks addresses it has confirmed; only those are matched to the allow list.
if not (st.user.get("email_verified") and access.is_allowed(current_user())):
    render_no_access()
    st.stop()
user = current_user()


# ── Sidebar ───────────────────────────────────────────────────
with st.sidebar:
    st.title(config.APP_NAME, anchor=False)
    st.caption(config.APP_TAGLINE)
    name = st.user.get("name")
    st.caption(f"Signed in as **{md(name)}**  \n{md(user)}" if name else f"Signed in as **{md(user)}**")
    if st.button("Sign out", type="tertiary", icon=":material/logout:"):
        st.logout()

    st.divider()

    # Credits display
    remaining = access.remaining(user)
    st.metric(
        label="Credits left today",
        value=f"{remaining} of {config.DAILY_LIMIT_PER_USER}",
    )
    st.progress(remaining / config.DAILY_LIMIT_PER_USER)

    if remaining == 0:
        st.error(
            f"No credits left today. They reset at midnight {config.LIMIT_TIMEZONE_NAME}. "
            "Your earlier results are still available below."
        )
    else:
        st.caption(
            "Each successful analysis uses one credit. Failed attempts are free. "
            f"Credits reset at midnight {config.LIMIT_TIMEZONE_NAME}."
        )

    st.divider()

    # Settings
    st.subheader("Settings", anchor=False)
    engine = st.radio(
        "Engine",
        list(config.ENGINES),
        index=list(config.ENGINES).index(config.DEFAULT_ENGINE),
        format_func=engine_label,
        key=config.ENGINE_KEY,
        horizontal=True,
        help="The AI engine that reads the reviews. Both use the same credits.",
    )
    max_reviews = st.slider(
        "Reviews to analyze",
        min_value=10,
        max_value=config.MAX_REVIEWS_TO_SCRAPE,
        value=config.DEFAULT_REVIEWS_TO_SCRAPE,
        step=10,
        help=(
            "The most reviews to collect from the web or take from pasted text. "
            "More reviews give a steadier result but take a little longer."
        ),
    )
    include_related = st.toggle(
        "Suggest alternatives",
        value=True,
        help="Adds a short extra step to each analysis. Does not use an extra credit.",
    )

    # History
    history = st.session_state[config.RESULTS_KEY]
    if history:
        st.divider()
        st.subheader("Recent analyses", anchor=False)
        current = st.session_state[config.CURRENT_KEY]
        for i in reversed(range(len(history))):
            st.button(
                history[i]["analysis"]["product_name"],
                key=f"history_{i}",
                help=history[i]["created_at"],
                on_click=select_result,
                args=(i,),
                type="primary" if i == current else "secondary",
                width="stretch",
            )
        st.button(
            "Clear history",
            on_click=clear_history,
            type="tertiary",
            icon=":material/delete_sweep:",
            help="Removes saved results. Does not restore credits.",
        )


# ── Main area ─────────────────────────────────────────────────
st.title("Check a product before you buy", anchor=False)
st.caption(
    "Enter a product to see what buyers like, what they complain about, "
    "a buying verdict and the best store price, drawn from public reviews."
)

api_ready = bool(ai_analyzer.get_api_key(engine))
if not api_ready:
    st.error(
        f"{engine_label(engine)} is not available: no API key is configured. Add it to "
        ".streamlit/secrets.toml (see secrets.toml.example) and reload the page, or "
        "choose another engine in the sidebar.",
        icon=":material/key_off:",
    )

source = st.radio(
    "Review source",
    [SOURCE_WEB, SOURCE_PASTE],
    format_func=SOURCE_LABELS.get,
    key="source_mode",
    horizontal=True,
    label_visibility="collapsed",
)

# Input form
manual_text = ""
with st.form("product_form"):
    name_input = dict(
        label="Product name",
        key="product_name",
        placeholder="e.g., Mahindra Thar, iPhone 15, boAt Airdopes 141",
        help="Include the brand and model for the most accurate match.",
        max_chars=120,
    )
    submit_button = dict(
        label="Analyze",
        type="primary",
        icon=":material/search:",
        width="stretch",
        disabled=not api_ready or remaining == 0,
    )

    if source == SOURCE_WEB:
        col1, col2 = st.columns([4, 1], vertical_alignment="bottom")
        with col1:
            product_name = st.text_input(**name_input)
        with col2:
            submitted = st.form_submit_button(**submit_button)
    else:
        product_name = st.text_input(**name_input)
        manual_text = st.text_area(
            "Reviews",
            key="manual_text",
            height=220,
            placeholder=(
                "Battery life is amazing, easily lasts two days with heavy use...\n\n"
                "Camera is average in low light and the phone heats up while gaming...\n\n"
                "Too expensive for what it offers compared to last year's model..."
            ),
            help="Separate reviews with a blank line, or number them (1. 2. 3.).",
        )
        submitted = st.form_submit_button(**submit_button)

# ── Handle submission ─────────────────────────────────────────
if submitted:
    new_index = run_analysis(
        product_name.strip(),
        source,
        manual_text,
        max_reviews,
        include_related,
        engine,
    )
    if new_index is not None:
        select_result(new_index)
        st.rerun()  # refresh the sidebar credit counter and history

# ── Current result ────────────────────────────────────────────
result = get_current_result()
if result:
    st.divider()
    clicked_related = render_result(result)
    if clicked_related:
        related_reviews_dialog(
            clicked_related,
            st.session_state[config.CURRENT_KEY],
            max_reviews,
            include_related,
            engine,
        )
elif not submitted:
    render_empty_state()

# ── Footer ────────────────────────────────────────────────────
st.divider()
st.caption(
    "AI-generated summary of public reviews. It can contain mistakes and prices change "
    "often, so check the store before buying."
)
