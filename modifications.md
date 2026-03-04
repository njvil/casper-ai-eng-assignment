# Project Modifications Log

A full record of all changes, investigations, plans, and insights made to this project after the initial commit. `scraper_v2.py` is untouched from its original state. All new functionality lives in new files or targeted pipeline additions.

---

## Files changed

| File | Status |
|------|--------|
| `src/scraper_v2.py` | **Unchanged** — original file, no modifications |
| `src/scraper_v3.py` | **New file** — standalone Playwright-based scraper |
| `src/llm_pipeline/pipeline.py` | **Modified** — `parse_reviews_data` extended to consume `featured_tweaks` |

---

## Investigation: Why `featured_tweaks` is always empty in `scraper_v2.py`

The original `scraper_v2.py` targets `<div class="photo-dialog__item">` elements to find featured tweaks. These exist in the page's photo gallery section, but the actual **"Featured Tweaks" section** uses a completely different component that is **client-side rendered by Vue/JavaScript**:

```
div.mm-recipes-ugc-threaded-carousel
  div.mm-recipes-ugc-threaded-carousel__card  (one per review)
```

A plain `requests.get` scraper only sees server-rendered HTML. The carousel is injected by JavaScript after page load, so it is never present in the response that `scraper_v2.py` parses. `featured_tweaks` is therefore always `[]`.

This was confirmed by comparing:
- `src/recipe.html` (browser-captured full page): 21 matches for `mm-recipes-ugc-threaded-carousel`
- `src/example_featured_tweaks.xml`: the exact HTML of one carousel
- Live HTTP request: returns HTTP 402 (blocked), confirming the site does not serve this content to plain scrapers

The old `photo-dialog__item` + `ugc-review` selectors also had a secondary flaw: they applied an additional `has_modification` regex filter on top of reviews that AllRecipes had already hand-curated as tweaks, silently dropping valid cards.

---

## New file: `src/scraper_v3.py`

A fully standalone Playwright-based scraper that replaces the HTTP fetch layer with a headless Chromium browser, making the JS-rendered Featured Tweaks carousel accessible.

**All parsing helpers are copied directly into the file** — it has no imports from `scraper_v2.py` and can be run independently.

### Key design decisions

- **`wait_until="load"`** (not `"networkidle"`): AllRecipes continuously fires background ad/analytics requests so `networkidle` never triggers and Playwright times out after 30 s. `"load"` waits for the browser's `window.load` event, which completes reliably.
- **`timeout=60_000`**: explicit 60 s timeout as a safety net for slow connections.
- **`page.wait_for_timeout(3_000)`**: 3 s settle time after load for Vue carousels to hydrate.
- **Blocks images/fonts/media** during load to reduce page load time.
- **Saves to the same `DATA_DIR`** (`project_root/data/`) as the rest of the project.

### Featured Tweaks HTML structure (from `src/example_featured_tweaks.xml`)

```
div.mm-recipes-ugc-threaded-carousel
  div.mm-recipes-ugc-threaded-carousel__card  [data-feedback-id="..."]
    span.mm-recipes-ugc-shared-card-byline__username-text
    div.mm-recipes-ugc-shared-star-rating
      span.mm-recipes-ugc-shared-star-rating__star  (×5)
        svg.ugc-shared-icon-star           ← filled
        svg.ugc-shared-icon-star-outline   ← empty
    span.mm-recipes-ugc-shared-card-meta__date
    div.mm-recipes-ugc-shared-item-card__text
    button.mm-recipes-ugc-shared-helpful-button   ← ends with integer count
    span.mm-recipes-ugc-shared-review-chips__text  ← optional tags e.g. "A keeper!"
```

Each extracted card gets `has_modification: true` and `is_featured: true` unconditionally — AllRecipes curates the carousel themselves.

### Output schema for each `featured_tweaks` entry

```json
{
  "feedback_id": "4818e356996b...",
  "username": "Jen Gerbrandt",
  "rating": 5,
  "date": "11/17/2005",
  "text": "I've tried a lot of different chocolate chip cookie recipes...",
  "helpful_count": 701,
  "chips": ["A keeper!", "Great flavors"],
  "is_featured": true,
  "has_modification": true
}
```

`chips` is optional and only present on newer reviews that use AllRecipes' structured tag system.

### Setup and usage

```bash
# One-time browser binary install
uv run playwright install chromium

# Run scraper
uv run python src/scraper_v3.py
```

### Confirmed working

`data/recipe_11679_homemade-mac-and-cheese.json` — 11 featured tweaks extracted.

---

## Pipeline change: `src/llm_pipeline/pipeline.py`

### Why the change is needed

`parse_reviews_data` originally only read from `recipe_data["reviews"]`. The pipeline hard-fails (returns `None`) if no review in that list has `has_modification: True`.

JSON files produced by `scraper_v3.py` may have a populated `featured_tweaks` list with reviews that all have `has_modification: True`, but an empty or sparse `reviews` list. Without the change, the pipeline would skip these recipes entirely.

### What changed

`parse_reviews_data` now iterates both `reviews` and `featured_tweaks`, appending featured tweak entries as `Review` objects with `has_modification=True`. Deduplication is handled via `feedback_id`:

```python
# Original reviews section (unchanged behaviour)
for review_data in recipe_data.get("reviews", []):
    ...

# Featured tweaks — always modifications, deduplicated by feedback_id
for tweak_data in recipe_data.get("featured_tweaks", []):
    feedback_id = tweak_data.get("feedback_id") or tweak_data["text"]
    if feedback_id not in seen_ids:
        seen_ids.add(feedback_id)
        reviews.append(Review(..., has_modification=True))
```

### Why this is safe to keep regardless of which scraper is used

- If `featured_tweaks` is `[]` (as it always is from `scraper_v2.py`), the loop is a no-op and behaviour is identical to before.
- If `featured_tweaks` is populated (from `scraper_v3.py`), those reviews are included in the pool the LLM draws from.
- There is no risk of double-counting: `reviews` and `featured_tweaks` come from different DOM sections and carry different `feedback_id` values.
