# ReviewLens

An AI product review analyzer built with Streamlit, with a choice of two AI engines.

Enter a product name and ReviewLens collects public reviews from the web, cleans
them, and asks an AI engine to summarise what buyers think: pros, cons, a sentiment
score, a user rating and a buying verdict. It also suggests related products worth
comparing.

## Features

- **Sign in with Google**: one click, no passwords to manage. Only emails on the
  allow list in `secrets.toml` get in (single addresses, or `@yourcompany.com` for a
  whole domain); everyone else sees "No access yet". Users stay signed in on their
  browser for 30 days, or until they sign out.
- **Daily credits**: each user gets 3 analyses a day across both engines, reset at
  midnight IST. A credit is taken when an analysis starts and given back if it
  fails, so two open tabs cannot use more than the limit.
- **Two engines**: pick Engine 1 or Engine 2 in the sidebar. Each result shows
  which engine made it.

  | Engine | Provider | Model |
  | --- | --- | --- |
  | Engine 1 (default) | DeepSeek | `deepseek-flash` (V4.1 Flash) |
  | Engine 2 | OpenAI | `gpt-5.4-mini` |

- **Web review collection**: meta-search for review pages, parallel download, and
  layered extraction (JSON-LD review data, review markup, then article text).
- **Manual mode**: paste reviews yourself when a product is hard to find online.
- **AI analysis**: the model first sorts every collected snippet into positive,
  mixed, negative or "not an opinion about this product" (menus, spec sheets,
  other models). The sentiment score, overall sentiment and review count are
  computed from that classification, so they always agree, and set-aside text is
  shown in its own tab. Each pro and con cites the reviews that make it and is
  shown with its mention count; uncited points are dropped. Confidence is capped
  by the amount of evidence (fewer than 5 genuine reviews is always Low).
- **User rating**: the median of ratings published in structured data on review
  pages, checked against the product name so a sidebar product's rating is not
  used. Ratings found only in page text need two pages to agree. Otherwise the
  rating is estimated from the review text and labelled as such.
- **Price comparison**: while reviews are collected, searches Amazon, Flipkart,
  Croma, Reliance Digital, Myntra and Snapdeal directly, plus brand stores and
  price-comparison sites found on the web. Shows one row per store with price, MRP,
  discount and stock, cheapest first, and filters out accessories and other models,
  including variants such as "Pro", "Plus" or "ANC" that the product name does not
  include. No credit or AI call is used.
- **Shops near you**: enter your area and city, or a PIN code, under **Your
  location** in the sidebar and each analysis also lists up to 6 physical shops near
  you that sell the product, authorised dealers first. The web is searched for
  dealers and store locators in your area (business directories such as Justdial
  and Sulekha, classifieds and online marketplaces are never used), the engine
  picks the shops on those pages, and every shop is then checked against its page
  in code: its name and full address must be written there, PIN code included, and
  the address must be in your area. A shop is shown only if it also passes at least
  one of these checks, each shown as a badge:
  - **Authorised dealer**: the page calls it an authorised, official or exclusive
    dealer, and the exact words are shown.
  - **Listed on brand site**: it is on the brand's own dealer locator (for example
    `dealers-auto.mahindra.com` for a Mahindra).
  - **On Google Maps**: the page links to its Google Maps business listing. A link
    that only drops a pin on coordinates does not count.
  - **Website**: the page is on its own site, or links to it.

  Phone numbers are kept only when they appear on the page. No extra credit is
  used; it is one more call to the engine. Leave the box empty to skip.
- **Value for money**: the engine rates the product Excellent, Good, Fair or Poor
  from the prices found and what reviewers say, with a one-line reason.
- **Related products**: three AI-suggested alternatives, chosen to address the
  product's weak points, each with an estimated price and how much cheaper or
  dearer it is. Click **Analyze reviews** on any of them to run a full review
  analysis in a popup without leaving the current result (uses 1 credit, free if
  already analyzed), then open it as the main view with a link back.
- **Export**: download any analysis as a Markdown report or raw JSON.
- **Session history**: results stay available until you sign out or reload the page.
  Credits are per account, so they are not reset by either.

The interface never names the model provider: engines are shown only as "Engine 1"
and "Engine 2", status steps and errors refer to "the analysis", and provider
details are only written to the server log.

## Setup

Requires Python 3.10 or newer.

```bash
pip install -r requirements.txt
```

Create the secrets file:

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

Then fill it in:

1. **API keys**: `DEEPSEEK_API_KEY` for Engine 1 (https://platform.deepseek.com/api_keys)
   and `OPENAI_API_KEY` for Engine 2 (https://platform.openai.com/api-keys). An
   engine without a key cannot be used; the other still works. Environment variables
   with the same names work too.
2. **Who may use the app**: list Google account emails in `[access] allowed_emails`.
   An entry like `"@yourcompany.com"` lets in everyone at that domain. To remove
   someone, delete their entry; it takes effect the next time they use the app.
3. **Google sign-in**: fill in `[auth]` as described below.

### Google sign-in

You need a Google OAuth client once. It is free.

1. Open the Google Auth Platform at https://console.cloud.google.com/auth/overview
   and pick or create a project.
2. **Branding**: enter the app name (shown on Google's sign-in screen) and a support
   email, then **Save**.
3. **Audience**: while the status is *Testing*, only the test users listed here can
   sign in, so add your own email under **Test users**. When others should sign in
   too, click **Publish app**. Publishing only lets Google show the sign-in screen;
   the allow list in `secrets.toml` still decides who gets in.
4. **Clients** > **Create client**: application type **Web application**. Under
   **Authorized redirect URIs** add `http://localhost:8501/oauth2callback`, then
   **Create**.
5. Copy the **Client ID** and **Client secret** into `[auth]` in `secrets.toml`, and
   set `cookie_secret` to a long random string:

   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

Open the app at exactly the address in `redirect_uri` (for local use,
http://localhost:8501, not the network IP address). When you deploy, add the
deployed `https://<your-app>/oauth2callback` to the Google client and set it as
`redirect_uri`.

## Run

```bash
streamlit run app.py
```

The app opens at http://localhost:8501.

## Project structure

| File | Purpose |
| --- | --- |
| `app.py` | Streamlit UI: sign-in, input form, analysis pipeline, result view, history |
| `config.py` | Settings, engines, prompts, and session state helpers |
| `access.py` | Sign-in setup check, email allow list, daily credits (SQLite) |
| `scraper.py` | Web search, page download, review and rating extraction, paste parser |
| `prices.py` | Price lookup, product matching, formatting and comparison |
| `stores.py` | Direct search adapters for Amazon, Flipkart, Croma, Reliance Digital, Myntra, Snapdeal |
| `ai_analyzer.py` | Engine clients, JSON-mode calls with retries, analysis normalisation |
| `local_shops.py` | Shops near the user: web search, page reading, and the checks each shop must pass |
| `related_products.py` | AI-suggested alternatives |
| `report.py` | Markdown and JSON export |

## Configuration

All tunable values live in `config.py`, including:

- `ENGINES`, `DEFAULT_ENGINE`: the label, API key name, endpoint, model and call
  parameters of each engine. Hidden reasoning is off for both (DeepSeek thinking
  disabled, OpenAI `reasoning_effort` set to `none`) because it is slower and costs
  more tokens without improving review summaries.
- `DAILY_LIMIT_PER_USER`, `LIMIT_TIMEZONE`: credits per user per day and the time
  zone whose midnight resets them.
- `SEARCH_RESULTS`, `SEARCH_REGION`: how many review pages to fetch and from which
  search region (for example `in-en` for India, `us-en` for the US).
- `PRICE_COUNTRY`, `PRICE_REGION`, `PRICE_CURRENCY`: the market prices are compared
  in. Defaults to India and INR; change all three together for another country.
- `PRICE_STORES`: stores searched directly. All are Indian stores; set it to `[]`
  for another country so only web-found listings are used.
- `LOCAL_SEARCH_RESULTS`, `LOCAL_PAGES`, `MAX_LOCAL_SHOPS`, `LOCAL_SKIP_DOMAINS`:
  how many shop pages to search for and send to the engine, how many shops to show,
  and the sites never used as a source of shops.
- `MAX_REVIEWS_TO_SCRAPE`, `MAX_TOTAL_CHARS`: limits that control cost and speed.
  Long reviews are trimmed to a fair share of `MAX_TOTAL_CHARS`, so every selected
  review is analyzed.
- `MIN_REVIEWS_FOR_MEDIUM_CONFIDENCE`, `MIN_REVIEWS_FOR_HIGH_CONFIDENCE`,
  `POSITIVE_THRESHOLD`, `NEGATIVE_THRESHOLD`: how confidence is capped and where the
  sentiment score turns Positive or Negative.
- `ANALYSIS_SYSTEM_PROMPT`, `RELATED_SYSTEM_PROMPT`, `LOCAL_SHOPS_SYSTEM_PROMPT`: the
  prompts sent to the engines.

## Limitations

- Many large stores (Amazon, Flipkart) load reviews with JavaScript or block
  scrapers, so web mode relies mostly on review sites, blogs and forums. Use
  paste mode for store reviews.
- Search engines rate-limit automated queries. If a search returns nothing,
  wait a minute and retry, or paste reviews.
- Credits are stored in `access.db` next to the app. On hosts whose disk is wiped
  on restart (such as Streamlit Community Cloud) the counts reset when the app
  restarts.
- Reloading the page keeps you signed in but clears the result history.
- Sign-in does not work when the app is embedded in another page (a Streamlit
  restriction).
- Prices come from public pages and can be out of date. Stores change their page
  layout from time to time; when that happens the affected store simply drops out
  of the comparison until its adapter in `stores.py` is updated. Tata CLiQ, JioMart
  and Vijay Sales load results with JavaScript and are not searched.
- Related products and their ratings and prices are AI suggestions. Verify them on the
  store before buying.
- Shops near you come only from pages a web search finds, so a genuine shop that has
  no page with its address in plain text (many store pages build the address, map
  link and phone number with JavaScript) is not shown. Distance is judged from the
  address text (locality, city or PIN code), not from a map, and "Your city" can
  include the outskirts of a district. The checks confirm what the pages say; they
  cannot confirm stock, price or that a shop is still open, so call before you go.
