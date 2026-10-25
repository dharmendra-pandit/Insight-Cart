# ai_analyzer.py
"""
AI analysis module.
Sends cleaned reviews to the chosen engine (config.ENGINES) and returns
structured JSON output. Also exposes `request_json()`, the shared low-level
call used by related_products.py.
"""

import os
import json
import logging

import openai
import streamlit as st
from openai import OpenAI

import config
import prices as price_lookup

log = logging.getLogger(__name__)

SENTIMENTS = ("Positive", "Mixed", "Negative")
CONFIDENCE_LEVELS = ("High", "Medium", "Low")
VALUE_LEVELS = ("Excellent", "Good", "Fair", "Poor", "Unknown")


# ── Client ────────────────────────────────────────────────────
def get_api_key(engine: str) -> str | None:
    """Read an engine's API key from Streamlit secrets, then the environment."""
    name = config.ENGINES[engine]["secret"]
    try:
        key = st.secrets.get(name)
    except Exception:  # no secrets.toml at all
        key = None
    return key or os.environ.get(name)


@st.cache_resource(show_spinner=False)
def _get_client(api_key: str, base_url: str | None) -> OpenAI:
    """Create an OpenAI-compatible client (one per API key and endpoint)."""
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=config.AI_TIMEOUT,
        max_retries=2,  # SDK retries connection errors, 429 and 5xx
    )


# ── Low-level call ────────────────────────────────────────────
def _friendly_api_error(e: Exception) -> str:
    """
    Turn an OpenAI SDK exception into a message a user can act on.
    Messages shown in the UI never name the model provider; details are logged.
    """
    if isinstance(e, openai.AuthenticationError):
        return "The analysis service rejected the API key. Check the key in .streamlit/secrets.toml."
    if isinstance(e, openai.RateLimitError):
        # OpenAI reports an empty balance as a rate limit with this code.
        if getattr(e, "code", None) == "insufficient_quota":
            return "The analysis service account has run out of balance."
        return "The analysis service is busy. Wait a minute and try again."
    if isinstance(e, openai.APITimeoutError):
        return "The analysis took too long. Try again with fewer reviews."
    if isinstance(e, openai.APIConnectionError):
        return "Could not reach the analysis service. Check your internet connection."
    if isinstance(e, openai.APIStatusError):
        if e.status_code == 402:
            return "The analysis service account has run out of balance."
        return f"The analysis service returned an error ({e.status_code}). Try again shortly."
    return "Something went wrong during the analysis. Try again."


def _safe_parse_json(raw: str) -> dict | None:
    """Parse JSON, stripping markdown fences if present."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Last resort: find first { ... last }
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def request_json(system_prompt: str, user_prompt: str, engine: str = config.DEFAULT_ENGINE,
                 attempts: int = 2, required: tuple[str, ...] = ()) -> tuple[dict | None, str | None]:
    """
    Call an engine in JSON mode and parse the reply.
    Retries on empty, truncated or unparseable output, and on replies missing
    any of the `required` keys.
    Returns (data, None) on success or (None, error_message) on failure.
    """
    settings = config.ENGINES[engine]
    api_key = get_api_key(engine)
    if not api_key:
        return None, "The analysis service is not configured: no API key was found."

    client = _get_client(api_key, settings["base_url"])
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    error = "The analysis came back empty. Try again."
    for attempt in range(1, attempts + 1):
        try:
            response = client.chat.completions.create(
                model=settings["model"],
                messages=messages,
                response_format={"type": "json_object"},
                **settings["params"],
            )
        except Exception as e:
            log.warning("%s (%s) call failed: %s", engine, settings["model"], e)
            return None, _friendly_api_error(e)

        choice = response.choices[0]
        content = (choice.message.content or "").strip()
        if choice.finish_reason == "length":
            error = "The analysis was cut off. Try again with fewer reviews."
            log.warning("attempt %d: response truncated", attempt)
            continue
        if not content:
            error = "The analysis came back empty. Try again."
            log.warning("attempt %d: empty response", attempt)
            continue

        data = _safe_parse_json(content)
        if data is None:
            log.warning("attempt %d: unparseable JSON: %s", attempt, content[:300])
        else:
            missing = [k for k in required if k not in data]
            if not missing:
                return data, None
            log.warning("attempt %d: reply is missing %s", attempt, missing)
        error = "The analysis came back in an unexpected format. Try again."

    return None, error


# ── Review prep ───────────────────────────────────────────────
def _prepare_reviews(reviews: list[str], user_rating=None) -> tuple[str, int]:
    """
    Format reviews into a numbered block for the prompt. Long reviews are
    trimmed to a fair share of config.MAX_TOTAL_CHARS so that every selected
    review is analyzed, instead of the last ones being dropped.
    Returns (text, number_of_reviews_included).
    """
    # 10 characters per review are left for the "[12] " prefix and a trailing "...".
    share = max(config.MIN_CHARS_KEPT_PER_REVIEW, config.MAX_TOTAL_CHARS // max(len(reviews), 1) - 10)
    blocks = []
    total = 0

    for i, r in enumerate(reviews, 1):
        text = r.strip()
        if len(text) > share:
            text = text[:share].rstrip() + "..."
        line = f"[{i}] {text}"
        if blocks and total + len(line) > config.MAX_TOTAL_CHARS:
            break
        blocks.append(line)
        total += len(line)

    text = "\n".join(blocks)

    if user_rating:
        text = f"Average rating published on review pages: {user_rating}/5\n\n" + text

    return text, len(blocks)


# ── Output normalisation ──────────────────────────────────────
def _to_float(value, low: float, high: float) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return min(max(value, low), high)


def _to_ids(value) -> list[int]:
    """Snippet numbers from [1, "2", "[3]"]; anything else is skipped."""
    if not isinstance(value, list):
        return []
    ids = []
    for v in value:
        try:
            ids.append(int(str(v).strip("[] ")))
        except ValueError:
            continue
    return ids


def _cited_points(value, valid_ids: set[int], limit: int = 6) -> tuple[list[str], list[int]]:
    """
    Pros or cons as (texts, mention_counts), most mentioned first.
    A point's count is the number of genuine reviews it cites. Uncited points
    are dropped, unless the model cited nothing at all (then they are kept
    without counts).
    """
    if isinstance(value, (str, dict)):
        value = [value]
    points = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            text = str(item.get("point") or item.get("text") or "").strip()
            mentions = len(set(_to_ids(item.get("reviews"))) & valid_ids)
        else:
            text, mentions = str(item).strip(), 0
        if text:
            points.append((text, mentions))

    if any(mentions for _, mentions in points):
        points = [p for p in points if p[1]]
    points.sort(key=lambda p: -p[1])  # stable: keeps the model's order on ties
    points = points[:limit]
    return [text for text, _ in points], [mentions for _, mentions in points]


def _pick(value, options: tuple[str, ...], fallback: str) -> str:
    text = str(value or "").strip().title()
    if text == "Neutral":
        text = "Mixed"
    return text if text in options else fallback


def _classify(data: dict, count: int) -> list[str | None]:
    """Sentiment of each snippet sent ("Positive", "Mixed", "Negative"), or None if it is not an opinion."""
    labels: list[str | None] = [None] * count
    for sentiment in SENTIMENTS:
        for i in _to_ids(data.get(f"{sentiment.lower()}_reviews")):
            if 1 <= i <= count and labels[i - 1] is None:
                labels[i - 1] = sentiment
    return labels


def _confidence(stated: str, relevant: int, sources: int | None) -> str:
    """The model's confidence, capped by how much evidence there is."""
    if relevant < config.MIN_REVIEWS_FOR_MEDIUM_CONFIDENCE:
        cap = "Low"
    elif relevant < config.MIN_REVIEWS_FOR_HIGH_CONFIDENCE or sources == 1:
        cap = "Medium"
    else:
        cap = "High"
    # CONFIDENCE_LEVELS runs from High to Low, so the later one is the lower.
    return max(stated, cap, key=CONFIDENCE_LEVELS.index)


# ── Main analyze function ─────────────────────────────────────
def analyze_reviews(product_name: str, reviews: list[str], user_rating=None,
                    prices: dict | None = None, review_sources: list[str] | None = None,
                    engine: str = config.DEFAULT_ENGINE) -> dict:
    """
    Send reviews (and any store prices found) to an engine and get structured analysis.
    `review_sources` is the page each review came from (None for pasted reviews).

    Sentiment, its score, the review count and the confidence cap are computed
    here from the model's per-review classification, so the numbers shown
    always agree with each other.
    Returns a dict with pros, cons, sentiment, value for money, verdict, etc.
    On failure returns {"error": str, "product_name": str}.
    """
    if not reviews:
        return {
            "error": "No reviews to analyze.",
            "product_name": product_name,
        }

    review_block, sent = _prepare_reviews(reviews, user_rating)
    user_prompt = (
        f"Product: {product_name}\n\n"
        f"{price_lookup.describe_for_prompt(prices)}\n\n"
        f"Reviews:\n{review_block}\n\n"
        f"Analyze these reviews and return JSON."
    )

    data, error = request_json(
        config.ANALYSIS_SYSTEM_PROMPT,
        user_prompt,
        engine,
        required=tuple(f"{s.lower()}_reviews" for s in SENTIMENTS),
    )
    if error:
        return {"error": error, "product_name": product_name}

    labels = _classify(data, sent)
    counts = {s: labels.count(s) for s in SENTIMENTS}
    relevant = sum(counts.values())
    if not relevant:
        return {
            "error": (
                f"None of the collected text was an opinion about {product_name}. "
                "Try the full name with brand and model, or paste reviews."
            ),
            "product_name": product_name,
        }

    score = (counts["Positive"] + 0.5 * counts["Mixed"]) / relevant
    if score >= config.POSITIVE_THRESHOLD:
        sentiment = "Positive"
    elif score <= config.NEGATIVE_THRESHOLD:
        sentiment = "Negative"
    else:
        sentiment = "Mixed"

    # Distinct pages the genuine opinions came from (unknown for pasted reviews).
    sources = None
    if review_sources and len(review_sources) >= sent:
        sources = len({review_sources[i] for i, label in enumerate(labels) if label})

    valid_ids = {i for i, label in enumerate(labels, 1) if label}
    pros, pro_mentions = _cited_points(data.get("pros"), valid_ids)
    cons, con_mentions = _cited_points(data.get("cons"), valid_ids)

    ai_rating = _to_float(data.get("average_user_rating"), 0.0, 5.0)
    # If the scraper found a rating, trust it over the AI estimate.
    if user_rating is not None:
        rating, rating_source = float(user_rating), "scraped"
    elif ai_rating:
        rating, rating_source = ai_rating, "ai_estimate"
    else:
        rating, rating_source = None, None

    return {
        "product_name": str(data.get("product_name") or product_name).strip(),
        "overall_sentiment": sentiment,
        "sentiment_score": round(score, 3),
        "sentiment_counts": counts,
        "review_sentiments": labels,
        "pros": pros,
        "pro_mentions": pro_mentions,
        "cons": cons,
        "con_mentions": con_mentions,
        "average_user_rating": round(rating, 1) if rating is not None else None,
        "rating_source": rating_source,
        "confidence": _confidence(
            _pick(data.get("confidence"), CONFIDENCE_LEVELS, "Low"), relevant, sources
        ),
        "value_for_money": _pick(data.get("value_for_money"), VALUE_LEVELS, "Unknown"),
        "value_reason": str(data.get("value_reason") or "").strip(),
        "buying_verdict": str(data.get("buying_verdict") or "No verdict generated.").strip(),
        "summary": str(data.get("summary") or "").strip(),
        # Genuine opinions only; snippets that were page clutter are not counted.
        "total_reviews_analyzed": relevant,
        "reviews_sent": sent,
        "source_count": sources,
    }
