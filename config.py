# config.py
"""
Central configuration for ReviewLens, the AI product review analyzer.
All constants, engine settings, prompts, and session state helpers live here.
"""

from datetime import timedelta, timezone
from pathlib import Path

import streamlit as st

# ── App ───────────────────────────────────────────────────────
APP_NAME = "ReviewLens"
APP_TAGLINE = "Product review analyzer"

# ── AI engines ────────────────────────────────────────────────
# Both providers offer an OpenAI-compatible chat API, so the OpenAI SDK
# serves both. The UI only ever shows an engine's label; the provider and
# model behind it stay on the server.
# "secret" is the name of the API key in .streamlit/secrets.toml or the
# environment. "params" are passed as-is to chat.completions.create().
ENGINES = {
    "engine1": {
        "label": "Engine 1",
        "secret": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",           # DeepSeek V4.1 Flash: fast + cheap
        "params": {
            "max_tokens": 4000,              # prevent JSON truncation
            "temperature": 0.3,              # low = more factual / consistent
            # Thinking mode spends output tokens on hidden reasoning and ignores
            # temperature. Summarising reviews does not need it, so keep it off.
            "extra_body": {"thinking": {"type": "disabled"}},
        },
    },
    "engine2": {
        "label": "Engine 2",
        "secret": "OPENAI_API_KEY",
        "base_url": None,                    # the OpenAI default
        "model": "gpt-5.4-mini",
        "params": {
            # Reasoning models take max_completion_tokens instead of max_tokens,
            # and hidden reasoning counts toward it. temperature is left out
            # because reasoning models may reject it.
            "max_completion_tokens": 4000,
            # "none" skips hidden reasoning, which review summaries do not need.
            "reasoning_effort": "none",
        },
    },
}
DEFAULT_ENGINE = "engine1"
AI_TIMEOUT = 90                            # seconds per API call

# ── Web search & scraping ─────────────────────────────────────
SEARCH_RESULTS = 8                         # review pages to fetch
SEARCH_REGION = "in-en"                    # ddgs region code ("in-en", "us-en", "wt-wt", ...)
SEARCH_ATTEMPTS = 3                        # retries when a search engine rate-limits us
# Web search engines ddgs may use. The encyclopedia backends (wikipedia,
# grokipedia) never return review or store pages, so they are left out.
SEARCH_BACKENDS = "duckduckgo,google,brave,yahoo,mojeek,startpage"
FETCH_TIMEOUT = 12                         # seconds per page
FETCH_WORKERS = 6                          # pages downloaded in parallel

MAX_REVIEWS_TO_SCRAPE = 60                 # cap to control cost & speed
DEFAULT_REVIEWS_TO_SCRAPE = 30
MIN_CHARS_PER_REVIEW = 40                  # shorter snippets are usually UI text
MAX_CHARS_PER_REVIEW = 800                 # truncate long reviews
MAX_TOTAL_CHARS = 32000                    # review text sent for analysis (~8k tokens)
MIN_CHARS_KEPT_PER_REVIEW = 300            # long reviews are trimmed to fit, never below this

# ── Price comparison ──────────────────────────────────────────
# Prices are searched for in one market so they can be compared.
PRICE_COUNTRY = "India"                    # used in the search query
PRICE_REGION = "in-en"                     # ddgs region code for price search
PRICE_CURRENCY = "INR"                     # listings in other currencies are dropped
PRICE_SEARCH_RESULTS = 10                  # store / price pages to fetch
MAX_PRICE_OFFERS = 12                      # listings shown in the table
# Stores searched directly (see stores.py). All are Indian stores selling in INR;
# set to [] when comparing prices in another country.
PRICE_STORES = ["amazon", "flipkart", "croma", "reliance_digital", "myntra", "snapdeal"]
STORE_MATCHES_CONSIDERED = 3               # cheapest of a store's first N matching results

# ── Access & credits ──────────────────────────────────────────
# Users sign in with Google ([auth] in .streamlit/secrets.toml). Only emails
# listed in [access] allowed_emails get in (see access.py and README.md).
DAILY_LIMIT_PER_USER = 3                   # successful analyses per user per day, all engines
# Credits reset at midnight in this time zone. India has no daylight
# saving, so a fixed offset is exact.
LIMIT_TIMEZONE = timezone(timedelta(hours=5, minutes=30))
LIMIT_TIMEZONE_NAME = "IST"                # shown in the UI
ACCESS_DB = Path(__file__).with_name("access.db")  # credits used per user per day

ENGINE_KEY = "engine"                      # session_state key (the engine picker)
RESULTS_KEY = "analysis_history"           # session_state key
CURRENT_KEY = "current_result_index"       # session_state key

# ── Accuracy ──────────────────────────────────────────────────
# Confidence is capped by how much evidence there is, whatever the model says.
MIN_REVIEWS_FOR_MEDIUM_CONFIDENCE = 5
MIN_REVIEWS_FOR_HIGH_CONFIDENCE = 12
# Sentiment score (share of positive reviews, mixed counting half) that
# makes the overall sentiment Positive or Negative; in between is Mixed.
POSITIVE_THRESHOLD = 0.65
NEGATIVE_THRESHOLD = 0.35

# ── UI ────────────────────────────────────────────────────────
PAGE_TITLE = f"{APP_NAME} - {APP_TAGLINE}"
PAGE_ICON = ":material/manage_search:"
LAYOUT = "wide"
RELATED_PRODUCTS_COUNT = 3

# ── Prompt Templates ──────────────────────────────────────────
# Kept here so you can tweak them without touching logic.
# JSON mode (both engines) requires the word "json" and an example in the prompt.
ANALYSIS_SYSTEM_PROMPT = """You are a product review analyst. You will receive numbered
text snippets ([1], [2], ...) collected from review sites, forums and store pages
for one product. Many snippets are not opinions about that product: website
boilerplate, spec sheets, marketing copy, news, or text about a different product,
model or generation. Those must be left out.

Work in two steps.
1. Decide which snippets are genuine opinions about the requested product (a buyer
   review, or a reviewer's hands-on verdict) and classify each one as positive,
   mixed or negative.
2. Fill in every other field using only those snippets. Do not add facts from your
   own knowledge, and do not repeat claims that no snippet makes.

Return ONLY valid JSON matching the example below. Use simple English.
Be concise. No markdown, no emoji, no extra text.

Field rules:
- product_name: the requested product, properly capitalized. Do not change the model.
- positive_reviews, mixed_reviews, negative_reviews: snippet numbers of the genuine
  opinions. Each number goes in at most one list; snippets that are not opinions
  about this product go in none.
- pros and cons: up to 6 points each, fewer if the reviews do not support more.
  Each point is a short phrase plus "reviews", the numbers of the snippets that make
  it. Merge points that say the same thing. Most mentioned first.
- average_user_rating: rating out of 5 that the opinions suggest, or null if unclear.
- confidence: "High", "Medium" or "Low", based on how many genuine opinions there
  are and how much they agree.
- value_for_money: one of "Excellent", "Good", "Fair", "Poor", "Unknown". Judge it
  from the prices provided (if any), the quality reviewers describe, and what
  reviewers say about the price. Use "Unknown" only if there is nothing to go on.
- value_reason: one short sentence explaining the value_for_money rating.
- buying_verdict: 1 to 2 sentences on who should buy it and who should skip it.
- summary: 2 to 3 sentences on what reviewers say overall.

EXAMPLE JSON OUTPUT (snippet 3 was a menu, so it is in no list):
{
  "product_name": "Example Product",
  "positive_reviews": [1, 2, 5, 6],
  "mixed_reviews": [4],
  "negative_reviews": [7],
  "pros": [
    {"point": "Long battery life", "reviews": [1, 2, 6]},
    {"point": "Comfortable grip", "reviews": [5]}
  ],
  "cons": [
    {"point": "Heats up while gaming", "reviews": [4, 7]}
  ],
  "average_user_rating": 4.2,
  "confidence": "Medium",
  "value_for_money": "Good",
  "value_reason": "Costs less than most rivals and the battery alone justifies the price.",
  "buying_verdict": "Worth buying if you need long battery life. Skip if you want a cool-running device.",
  "summary": "Users love the battery but complain about heat."
}"""

RELATED_SYSTEM_PROMPT = f"""You are a product recommendation assistant.
Suggest {RELATED_PRODUCTS_COUNT} related products that are similar to the given product
and are well reviewed. Only suggest real products that exist and are currently sold
in the country named in the request. Prefer the same
category and a similar price range, and include at least one option that is
cheaper or better value. Do not suggest the given product itself.

Return ONLY valid JSON matching the example below. Use simple English.
No markdown, no emoji. ai_suggested_rating is your estimate of the typical user
rating out of 5. approx_price is the typical current selling price as a plain
number in the currency named in the request, or null if you are not sure.
why_similar must agree with approx_price and the current price given: only call
a product cheaper, similarly priced or pricier if the numbers say so.

EXAMPLE JSON OUTPUT:
{{
  "related_products": [
    {{
      "name": "Product B",
      "why_similar": "Same price range, better camera",
      "ai_suggested_rating": 4.5,
      "approx_price": 1299
    }}
  ]
}}"""


# ── Helpers: session state ────────────────────────────────────
# Credits are not kept here: they are per user per day, in access.py.
def init_session():
    """Initialize history in Streamlit session state if not present."""
    if RESULTS_KEY not in st.session_state:
        st.session_state[RESULTS_KEY] = []

    if CURRENT_KEY not in st.session_state:
        st.session_state[CURRENT_KEY] = None
