# related_products.py
"""
Related-product suggestions.
Asks the chosen engine for similar, well-reviewed alternatives to the analyzed
product. Ratings and prices here are AI estimates, not scraped data,
and the UI labels them that way.
"""

from urllib.parse import quote_plus

import config
from ai_analyzer import request_json
from prices import format_price


def search_url(product_name: str) -> str:
    """Web search link so the user can verify a suggestion."""
    return f"https://www.google.com/search?q={quote_plus(product_name + ' reviews')}"


def _build_prompt(product_name: str, analysis: dict | None, prices: dict | None,
                  currency: str) -> str:
    """Give the model what reviewers liked and disliked, and the price, so suggestions address it."""
    lines = [f"Product: {product_name}"]
    if prices and prices.get("lowest"):
        lines.append(f"Current price: about {format_price(prices['lowest']['price'], currency)}")
    if analysis:
        if analysis.get("summary"):
            lines.append(f"Review summary: {analysis['summary']}")
        if analysis.get("pros"):
            lines.append("Liked: " + "; ".join(analysis["pros"]))
        if analysis.get("cons"):
            lines.append("Disliked: " + "; ".join(analysis["cons"]))
    lines.append(
        f"\nSuggest {config.RELATED_PRODUCTS_COUNT} related products sold in {config.PRICE_COUNTRY}. "
        "Where possible, pick ones that fix the disliked points. "
        f"Give approx_price in {currency}. Return JSON."
    )
    return "\n".join(lines)


def _positive_number(value) -> float | None:
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _normalize(item, currency: str) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    if not name:
        return None

    rating = _positive_number(item.get("ai_suggested_rating"))
    rating = round(rating, 1) if rating and rating <= 5 else None

    return {
        "name": name,
        "why_similar": str(item.get("why_similar") or "").strip(),
        "ai_suggested_rating": rating,
        "approx_price": _positive_number(item.get("approx_price")),
        "currency": currency,
        "search_url": search_url(name),
    }


def get_related_products(product_name: str, analysis: dict | None = None,
                         prices: dict | None = None, engine: str = config.DEFAULT_ENGINE) -> dict:
    """
    Returns:
      {
        "related_products": [
          {"name": str, "why_similar": str, "ai_suggested_rating": float | None,
           "approx_price": float | None, "currency": str, "search_url": str},
          ...
        ],
        "error": str | None,
      }
    """
    # Estimate in the same currency as the scraped prices so they can be compared.
    currency = (prices or {}).get("currency") or config.PRICE_CURRENCY
    data, error = request_json(
        config.RELATED_SYSTEM_PROMPT,
        _build_prompt(product_name, analysis, prices, currency),
        engine,
    )
    if error:
        return {"related_products": [], "error": error}

    items = data.get("related_products")
    products = [
        p for p in (_normalize(item, currency) for item in (items if isinstance(items, list) else []))
        if p
    ]

    # The model occasionally suggests the product itself; drop it.
    target = product_name.strip().lower()
    products = [p for p in products if p["name"].lower() != target]

    if not products:
        return {"related_products": [], "error": "No related products were suggested."}
    return {"related_products": products[: config.RELATED_PRODUCTS_COUNT], "error": None}
