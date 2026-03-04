# Recipe Enhancement Platform — Full Engineering Documentation

This document provides a comprehensive, chronological account of the entire engineering process for improving the Recipe Enhancement Platform: every assumption made, problem encountered, line of reasoning followed, technical decision taken, and challenge overcome. It is written to be read by an engineer who has no prior context.

---

## Table of Contents

1. [Project Context and Starting Point](#1-project-context-and-starting-point)
2. [Initial Assumptions and Setup](#2-initial-assumptions-and-setup)
3. [Problem Analysis Phase](#3-problem-analysis-phase)
4. [Solution Approach — Scraper](#4-solution-approach--scraper)
5. [Solution Approach — Pipeline v2](#5-solution-approach--pipeline-v2)
6. [Technical Decisions and Rationale](#6-technical-decisions-and-rationale)
7. [Implementation Details and Challenges Overcome](#7-implementation-details-and-challenges-overcome)
8. [Final Architecture](#8-final-architecture)
9. [Enhanced Output Schema](#9-enhanced-output-schema)
10. [Scalability Analysis](#10-scalability-analysis)
11. [Logging](#11-logging)
12. [Future Improvements](#12-future-improvements)
13. [File Inventory](#13-file-inventory)

---

## 1. Project Context and Starting Point

The Recipe Enhancement Platform is a system that:

1. **Scrapes** recipe data (ingredients, instructions, reviews) from AllRecipes.com.
2. **Extracts** structured modifications from community reviews using an LLM.
3. **Applies** those modifications to the original recipe.
4. **Outputs** an enhanced recipe with full attribution tracking.

The intended user experience is that a person can view an enhanced recipe and **inspect line-level diffs** explaining exactly which community suggestions were applied, who suggested them, and why.

### Starting codebase

The project was received with the following pre-existing code:

| File | Purpose |
|------|---------|
| `src/scraper_v2.py` | Scrapes AllRecipes using `requests` + BeautifulSoup |
| `src/llm_pipeline/pipeline.py` | Orchestrates the 3-step enhancement pipeline |
| `src/llm_pipeline/tweak_extractor.py` | Uses OpenAI to extract modifications from reviews |
| `src/llm_pipeline/recipe_modifier.py` | Applies modifications via fuzzy string matching |
| `src/llm_pipeline/enhanced_recipe_generator.py` | Packages the enhanced recipe with attribution |
| `src/llm_pipeline/models.py` | Pydantic data models |
| `src/llm_pipeline/prompts.py` | LLM prompts and few-shot examples |
| `src/test_pipeline.py` | Manual test runner |
| `data/recipe_*.json` | 16 scraped recipe files |
| `data/enhanced/enhanced_*.json` | 2 pre-existing enhanced recipe samples |

### Pre-existing data

- 16 recipe JSON files in `data/`, scraped at various dates.
- 2 enhanced recipe JSONs in `data/enhanced/`, which upon analysis turned out to be **hand-crafted samples** (see Section 3 for the evidence).

---

## 2. Initial Assumptions and Setup

### Environment assumptions

- The project uses Python 3.13+ with `uv` as the package manager.
- The `.env` file contains an `OPENAI_API_KEY` for the LLM calls.
- Playwright was already listed as a dependency in `pyproject.toml` but was not used.

### Initial scraper issues

When first attempting to run `scraper_v2.py`, the scraper failed because a **VPN was active** on the development machine. AllRecipes blocked the request, returning HTTP errors. This was documented by trying to explore alternate ways of scraping the data, saving the "fixed" scraper state as `src/scraper_v2_initialissues.py` for reference. After disabling the VPN, the scraper functioned normally — it could fetch recipe pages and extract JSON-LD structured data, reviews, and metadata.

### Reference material gathered

To understand what the scraper should be capturing, the following reference files were created:

- **`src/data/references/recipe.html`** (6,362 lines): The complete rendered HTML of the chocolate chip cookie recipe page, captured from a browser with JavaScript fully executed. This served as ground truth for what elements exist on the page.
- **`src/data/references/example_featured_tweaks.xml`** (3 lines, single long line): The specific `<div class="mm-recipes-ugc-threaded-carousel">` section extracted from the above HTML. This is the "Featured Tweaks" carousel that AllRecipes displays on recipe pages — a curated set of the most useful community modifications.

These reference files were critical for understanding the gap between what `scraper_v2.py` sees (server-rendered HTML) and what a browser user sees (fully rendered page with JS components).

---

## 3. Problem Analysis Phase

### Problem 1: `scraper_v2.py` saves files to the wrong directory

**Discovery**: Running `scraper_v2.py` from inside `src/` caused files to be saved to `src/data/` instead of the project root's `data/` directory. The `save_recipe_data` function used hardcoded relative paths (`"data/..."`) that resolved against the current working directory.

**Root cause**: `os.makedirs("data", ...)` and `f"data/recipe_{id}_{slug}.json"` are CWD-relative. When the CWD is `src/`, the output goes to `src/data/`.

### Problem 2: `featured_tweaks` is always empty

**Discovery**: Every scraped recipe JSON had `"featured_tweaks": []`. The scraper was supposed to extract these from the page but never found any.

**Investigation**: The scraper targeted `<div class="photo-dialog__item">` elements containing `<div class="ugc-review">` — these are **photo gallery** elements, not the Featured Tweaks section. Additionally, even if it found reviews there, it applied a `has_modification` regex filter that would silently drop valid tweaks that AllRecipes had already curated.

**Key finding**: Searching `recipe.html` revealed:
- `photo-dialog__item`: 91 matches — these are in the photo gallery section.
- `mm-recipes-ugc-threaded-carousel`: 21 matches — this is the **actual** Featured Tweaks carousel.

The selectors in `scraper_v2.py` were targeting the wrong DOM section entirely.

### Problem 3: Featured Tweaks carousel is JS-rendered

**Discovery**: Even after correcting the selectors to target `mm-recipes-ugc-threaded-carousel`, the scraper still found nothing.

**Root cause**: The Featured Tweaks carousel is rendered by **Vue.js on the client side**. When `requests.get` fetches the page, it receives only the server-rendered HTML — the carousel `<div>` does not exist in that response. It is injected by JavaScript after the page loads in a browser.

**Confirmation**: An attempt to fetch the page from the development environment returned HTTP 402 ("Payment Required"), further confirming that AllRecipes blocks non-browser clients from certain content.

### Problem 4: The pipeline only processes one random review

**Discovery**: `process_single_recipe` in `pipeline.py` calls `extract_single_modification`, which uses `random.choice` to pick a single review and extract one `ModificationObject` from it.

**Evidence**: The pre-existing `enhanced_10813_best-chocolate-chip-cookies.json` contains **two** `modifications_applied` entries from **two** different reviews — but the current code can only produce **one** per run. Additionally, it contains a `confidence_score` field that doesn't exist in any Pydantic model. This confirmed the sample was hand-crafted, not produced by the pipeline.

### Problem 5: Multi-modification reviews are truncated

**Discovery**: The second review in the cookie recipe data contains 4 distinct modifications:
1. Changed sugar ratios (quantity_adjustment)
2. Omitted water (removal)
3. Added cream of tartar (addition)
4. Refrigerated batter (technique_change)

The `ModificationObject` schema forces a single `modification_type` enum. The LLM picked `quantity_adjustment` and only extracted the sugar changes. The other 3 modifications were silently lost.

### Problem 6: `has_modification` regex has high false-positive rate

**Discovery**: The 5 regex patterns used to flag reviews as containing modifications have significant false positives:
- `"I used this recipe as-is"` matches because of `"I used"`
- `"will make again without changes"` matches because of `"will make again"`
- `"more or less followed the recipe"` matches because of `"more ... "`

These reviews get `has_modification: True` and enter the LLM extraction pipeline, wasting API calls and potentially producing empty or nonsensical modifications.

### Problem 7: Few-shot examples exist but are unused

**Discovery**: `prompts.py` contains `build_few_shot_prompt` with 4 well-crafted examples covering quantity adjustment, addition+removal, salt adjustment, and technique change. But `tweak_extractor.py` calls `build_simple_prompt` instead, which sends no examples to the LLM.

### Problem 8: Safety validation exists but is never called

**Discovery**: `RecipeModifier.validate_modification_safety` checks whether `find` strings exist in the recipe before applying edits. It was fully implemented but never invoked by the pipeline. Instead, `apply_edit` silently warns and skips when fuzzy matching fails.

### Problem 9: No conflict resolution between contradictory reviews

**Discovery**: If multiple reviews are processed (which the v1 pipeline doesn't do, but v2 would need to), modifications may conflict — e.g. one reviewer says "more salt" while another says "less salt". There was no mechanism to detect or resolve such conflicts.

---

## 4. Solution Approach — Scraper

### Decision: Create a new file instead of modifying `scraper_v2.py`

**Rationale**: `scraper_v2.py` was the original assignment code. Modifying it risks breaking the original functionality and makes it harder to compare original vs improved behaviour. A new standalone file (`scraper_v3.py`, originally named `scraper_playwright.py`) was created instead.

**Key design decision — no imports from `scraper_v2.py`**: Initially the new scraper imported parsing helpers from `scraper_v2.py`. This was changed to make it fully standalone with all methods copied in. Reasoning: the two scrapers serve different purposes (plain HTTP vs headless browser), may diverge over time, and a standalone file is easier to understand and maintain.

### Decision: Use Playwright with headless Chromium

**Rationale**: The Featured Tweaks carousel is rendered by Vue.js. There are three options:
1. **Headless browser** (Playwright/Selenium) — executes JavaScript, sees the full rendered page.
2. **Reverse-engineer the API** — find the JSON endpoint AllRecipes uses internally.
3. **Accept empty `featured_tweaks`** — rely only on base reviews.

Option 1 was chosen because Playwright was already a dependency, it's the most reliable approach (works even if AllRecipes changes their API), and it captures everything a real user would see.

### Challenge: `networkidle` timeout

**Problem**: The initial implementation used `page.goto(url, wait_until="networkidle")`. AllRecipes continuously fires background ad/analytics requests, so the network never goes truly idle. Playwright timed out after 30 seconds.

**Solution**: Changed to `wait_until="load"` (waits for the browser's `window.load` event) with an explicit 60-second timeout, plus `page.wait_for_timeout(3_000)` for Vue carousels to finish hydrating.

### Performance optimisation: Block non-essential resources

The scraper routes all requests through a filter that aborts `image`, `font`, and `media` resource types. This significantly reduces page load time since AllRecipes serves large hero images and multiple ad-related media files.

### Selector mapping for Featured Tweaks carousel

From the reference HTML (`example_featured_tweaks.xml`), the following CSS selectors were identified:

| Field | Selector |
|-------|----------|
| Card container | `div.mm-recipes-ugc-threaded-carousel__card` |
| Unique ID | `data-feedback-id` attribute |
| Username | `span.mm-recipes-ugc-shared-card-byline__username-text` |
| Rating (filled stars) | `span.mm-recipes-ugc-shared-star-rating__star` not containing `svg.ugc-shared-icon-star-outline` |
| Date | `span.mm-recipes-ugc-shared-card-meta__date` |
| Review text | `div.mm-recipes-ugc-shared-item-card__text` |
| Helpful count | Last integer in `button.mm-recipes-ugc-shared-helpful-button` text |
| Tags/chips | `span.mm-recipes-ugc-shared-review-chips__text` |

All cards in the carousel get `has_modification: true` and `is_featured: true` unconditionally — AllRecipes curates this section themselves.

### Confirmed working

After implementing and running `scraper_v3.py`:
- `data/recipe_11679_homemade-mac-and-cheese.json`: 11 featured tweaks extracted.
- `data/recipe_10813_best-chocolate-chip-cookies copy.json`: featured tweaks extracted with usernames, ratings, helpful counts, and review chips.

---

## 5. Solution Approach — Pipeline v2

### Core design question: How to handle multiple reviews

The original pipeline picked one random review. The improved pipeline needed to process all reviews and produce a merged, deduplicated set of the best modifications. Three approaches were considered:

1. **Process all reviews, pick top N by rating** — simple but doesn't handle duplicates or conflicts.
2. **Process all reviews, group by target ingredient** — mechanical grouping, misses semantic similarity.
3. **Two-phase LLM: extract all, then summarise** — uses the LLM's semantic understanding to merge duplicates and resolve conflicts.

**Decision**: Option 3. A second LLM call handles the semantic heavy lifting — it understands that "reduce white sugar to half a cup" and "I only used 1/2 cup white sugar" are the same modification, and that "more sugar" conflicts with "less sugar".

### Data source priority

**Decision**: Use `featured_tweaks` as the primary source; fall back to `reviews` only if `featured_tweaks` is empty.

**Rationale**: Featured tweaks are AllRecipes-curated. They're selected by the platform as the most useful modifications, carry structured metadata (helpful count, chips/tags), and are more likely to describe concrete, successful changes. Base reviews include many that say "great recipe, made it as-is!" with a false-positive `has_modification` flag.

### Conflict resolution rules

When modifications conflict (e.g. "more sugar" vs "less sugar" targeting the same ingredient line):

1. **Prefer the one from a featured tweak** (`is_featured: true`) — curated by AllRecipes.
2. If both/neither are featured, **prefer higher rating** — the reviewer's overall satisfaction.
3. If ratings tie, **prefer higher helpful count** — community-validated usefulness.
4. Discard the losing modification entirely — don't apply contradictory changes.

This logic is implemented in the Phase 2 summarisation prompt, not in Python code. The LLM applies these rules semantically.

### Top 5 limit

**Decision**: Apply at most 5 modifications per recipe.

**Rationale**: More than 5 modifications risks transforming the recipe so much that it's no longer recognisable as the original. 5 is enough to capture the most impactful community-validated changes while keeping the recipe's identity intact. The top 5 come from the semantically summarised global list, not from individual reviews.

---

## 6. Technical Decisions and Rationale

### Few-shot prompting over simple prompting

**Before**: `tweak_extractor.py` called `build_simple_prompt` — no examples.
**After**: Calls `build_few_shot_prompt` with 3 diverse examples covering quantity adjustment, addition + removal, and technique change.

**Rationale**: The few-shot examples were already written and tested in `prompts.py` but never used. They directly address the most common extraction patterns seen in the cookie recipe data. Including them reduces LLM errors, especially for multi-modification reviews where the LLM needs to understand it should return multiple objects.

### Multi-object extraction per review

**Before**: `ModificationObject` is a single object with one `modification_type`. The LLM returns one object per review.
**After**: The LLM returns `{"modifications": [...]}` — a list of `ModificationObject` items per review.

**Rationale**: A single review like "(1) changed sugar ratios (2) omitted water (3) added cream of tartar (4) refrigerated batter" contains 4 distinct modifications of 4 different types. Forcing the LLM to pick one type and one set of edits silently drops 75% of the information.

### `ExtractionResult` wrapper model

Rather than changing `ModificationObject` itself, a new `ExtractionResult(BaseModel)` wraps `List[ModificationObject]`. This keeps `ModificationObject` clean and reusable while allowing the LLM to return multiple items.

### `RankedModification` model

The Phase 2 summarisation returns a different shape than Phase 1. `RankedModification` extends the concept with:
- `modification_type: List[...]` — a modification that spans multiple categories.
- `mention_count: int` — how many reviews suggested this same change.
- `best_source: BestSource` — metadata from the highest-quality source review.

### Safety validation before applying

**Before**: `validate_modification_safety` existed but was never called. `apply_edit` silently warned and skipped on fuzzy-match failure.
**After**: `apply_modifications_batch` calls `validate_modification_safety` before each modification. Unsafe modifications are skipped with a logged warning.

**Rationale**: It's better to skip a modification entirely and log why, than to partially apply it and produce a corrupted recipe.

### Similarity threshold raised from 0.6 to 0.7

**Rationale**: At 0.6, `SequenceMatcher` can match the wrong ingredient line. For example, "1 cup white sugar" at 0.6 threshold might match "1 cup packed brown sugar". Raising to 0.7 reduces false matches while still allowing for minor wording differences between the LLM's `find` text and the actual recipe line.

### Line-level diffs as first-class output

**Decision**: Add `LineDiff` as a Pydantic model and `line_diffs: List[LineDiff]` to `EnhancedRecipe`.

**Rationale**: The intended user experience is viewing line-level diffs inline. The `ChangeRecord` model already tracks changes, but it's keyed by what the modifier *attempted* to do, not by what *actually changed* in the recipe. `LineDiff` compares original vs final recipe element-wise and provides the exact data a UI needs: section, line index, original text, modified text, operation type, source reviewer, and reasoning.

### Original recipe preserved in enhanced output

**Decision**: Add `original_ingredients` and `original_instructions` to `EnhancedRecipe`.

**Rationale**: Without these, a UI rendering the enhanced recipe needs to also load the original JSON file to show a side-by-side comparison. Embedding the originals in the enhanced output makes it self-contained.

---

## 7. Implementation Details and Challenges Overcome

### Challenge 1: Scraper saving to wrong directory

**Fix**: Added `PROJECT_ROOT = Path(__file__).resolve().parent.parent` and `DATA_DIR = PROJECT_ROOT / "data"` at module level. All file operations use `DATA_DIR` instead of relative `"data/"` paths. This was applied to `scraper_v3.py`; `scraper_v2.py` was left untouched as the original.

### Challenge 2: Wrong CSS selectors for Featured Tweaks

**Process**: Compared `recipe.html` (browser-captured) against the selectors in `scraper_v2.py`. The scraper used `photo-dialog__item` (photo gallery) instead of `mm-recipes-ugc-threaded-carousel` (featured tweaks carousel). Mapped every field in the carousel card to a CSS selector using the reference HTML.

### Challenge 3: JS-rendered content not visible to `requests`

**Process**: After correcting selectors, `featured_tweaks` was still empty. Hypothesis: the carousel is client-side rendered. Confirmed by:
1. Searching `recipe.html` for `mm-recipes-ugc-threaded-carousel` → 21 matches (browser-rendered HTML has it).
2. Attempting a live HTTP fetch → HTTP 402 (blocked) or missing carousel in response.

**Solution**: Created `scraper_v3.py` using Playwright headless Chromium.

### Challenge 4: Playwright `networkidle` timeout

**Problem**: `page.goto(url, wait_until="networkidle")` timed out after 30s on AllRecipes due to continuous ad/analytics network activity.

**Solution**: `wait_until="load"` + `timeout=60_000` + `page.wait_for_timeout(3_000)`.

### Challenge 5: Pipeline only processes one review

**Process**: Traced the code path:
- `pipeline.py:168` → `extract_single_modification` → `random.choice(modification_reviews)` → one review.
- `enhanced_recipe_generator.py:138` → `modifications_applied = [modification_applied]` → always a list of 1.
- Confirmed the existing enhanced JSON was hand-crafted by finding a `confidence_score` field that doesn't exist in any model.

**Solution**: Rewrote the pipeline with two-phase extraction, batch application, and multi-modification support.

### Challenge 6: LLM returns single modification for multi-mod reviews

**Process**: The review "(1) changed sugar ratios (2) omitted water (3) added cream of tartar (4) refrigerated batter" was being reduced to just the sugar change because `ModificationObject` only has one `modification_type`.

**Solution**: Changed the prompt to request `{"modifications": [...]}` and added `ExtractionResult` model. Added explicit instruction in the prompt: "If the review contains multiple distinct modifications, return each as a separate object."

### Challenge 7: Deduplication and conflict resolution

**Process**: When processing all reviews, the same modification appears in many: dozens of reviewers say "use more brown sugar". And some reviews conflict: one says "more salt" while another says "less salt".

**Decision**: Let the LLM handle semantic deduplication and conflict resolution in a dedicated Phase 2 call, rather than trying to implement fragile string-matching heuristics in Python.

**Implementation**: `build_summarize_prompt` sends the full pool of extracted modifications (with source metadata) to the LLM and asks it to merge duplicates, resolve conflicts using the priority rules, and return the ranked top 5.

### Challenge 8: Pairing ranked modifications back to source reviews

After Phase 2 returns `RankedModification` objects with `best_source.username`, we need to find the original `Review` object for attribution. The extractor builds a `review_lookup: Dict[str, Review]` keyed by username during Phase 1, then looks up each ranked modification's `best_source.username` after Phase 2.

---

## 8. Final Architecture

### Data flow

```
scraper_v3.py (Playwright headless Chromium)
    │
    ▼
data/recipe_*.json  (ingredients, instructions, featured_tweaks, reviews)
    │
    ▼
pipeline.py v2.0.0
    │
    ├─ parse_reviews_data()
    │   └─ Prefer featured_tweaks; fall back to reviews
    │
    ├─ Phase 1: extract_modification() × N reviews
    │   └─ LLM returns ExtractionResult { modifications: [...] }
    │   └─ Tag each mod with source metadata
    │   └─ Build flat global pool
    │
    ├─ Phase 2: summarize_modifications()
    │   └─ LLM merges duplicates, resolves conflicts, ranks
    │   └─ Returns top 5 RankedModifications
    │
    ├─ Validate safety for each ranked modification
    │   └─ Skip unsafe ones
    │
    ├─ apply_modifications_batch()
    │   └─ Fuzzy match + apply edits sequentially
    │
    ├─ build_line_diffs()
    │   └─ Compare original vs modified line by line
    │
    └─ generate_enhanced_recipe()
        └─ Package with attribution, line diffs, originals
            │
            ▼
        data/enhanced/enhanced_*.json
```

### Module responsibilities

| Module | v1 Responsibility | v2 Responsibility |
|--------|------------------|------------------|
| `models.py` | Basic Recipe, Review, ModificationObject | + ExtractionResult, RankedModification, BestSource, LineDiff |
| `prompts.py` | Simple prompt (unused few-shot) | Few-shot extraction prompt + summarisation prompt |
| `tweak_extractor.py` | Pick 1 random review, extract 1 mod | Extract all mods from all reviews, summarise, rank top 5 |
| `recipe_modifier.py` | Apply 1 mod (no safety check) | Apply N mods with safety validation, threshold 0.7 |
| `enhanced_recipe_generator.py` | Package 1 mod | Package N mods + line diffs + original lists |
| `pipeline.py` | 3-step linear | 2-phase LLM + batch apply + diff builder |
| `test_pipeline.py` | Manual runner, no assertions | + prints diffs, asserts diffs exist |

---

## 9. Enhanced Output Schema

The final JSON a user/UI receives:

```json
{
  "recipe_id": "10813_enhanced",
  "original_recipe_id": "10813",
  "title": "Best Chocolate Chip Cookies (Community Enhanced)",

  "original_ingredients": ["1 cup butter, softened", "1 cup white sugar", "..."],
  "ingredients": ["1 cup butter, softened", "0.5 cup white sugar", "..."],

  "original_instructions": ["Preheat the oven to 350...", "..."],
  "instructions": ["Preheat the oven to 350...", "..."],

  "line_diffs": [
    {
      "section": "ingredients",
      "line_index": 1,
      "original": "1 cup white sugar",
      "modified": "0.5 cup white sugar",
      "operation": "replace",
      "source_username": "Jen Gerbrandt",
      "reasoning": "Increases brown-to-white sugar ratio for chewier texture"
    }
  ],

  "modifications_applied": [
    {
      "source_review": {
        "text": "I've tried a lot of different chocolate chip cookie recipes...",
        "reviewer": "Jen Gerbrandt",
        "rating": 5
      },
      "modification_type": ["quantity_adjustment"],
      "reasoning": "Increases brown-to-white sugar ratio for chewier texture",
      "changes_made": [
        {
          "type": "ingredient",
          "from_text": "1 cup white sugar",
          "to_text": "0.5 cup white sugar",
          "operation": "replace"
        }
      ],
      "mention_count": 3
    }
  ],

  "enhancement_summary": {
    "total_changes": 3,
    "change_types": ["quantity_adjustment", "removal"],
    "expected_impact": "Chewier texture with richer flavour from increased brown sugar ratio"
  },

  "pipeline_version": "2.0.0"
}
```

### How a UI would use `line_diffs`

Each `LineDiff` gives the UI everything needed to render an inline diff:
- **`section`**: which tab/area (ingredients vs instructions).
- **`line_index`**: which line in the original list.
- **`original` / `modified`**: the before/after text for that line.
- **`operation`**: `replace`, `add`, or `remove` — determines visual styling (highlight, green insert, red strikethrough).
- **`source_username`**: who suggested it — shown as an avatar or citation.
- **`reasoning`**: a tooltip or expandable explanation of why this change improves the recipe.

---

## 10. Scalability Analysis

### LLM cost per recipe

- **Phase 1**: N calls (one per review with modifications). Typically 5-15 reviews per recipe.
- **Phase 2**: 1 call (summarisation of the full pool).
- **Total**: ~6-16 calls per recipe at ~$0.001/call with gpt-3.5-turbo = **~$0.01-0.02 per recipe**.
- For 1,000 recipes: ~$10-20 in API costs. Highly acceptable.

### Context window

Phase 2 receives the full pool as input. With 15 reviews x ~3 mods each = ~45 modification objects, each ~200 tokens = ~9,000 tokens. gpt-3.5-turbo's context window is 16,384 tokens. Fits comfortably. For recipes with extremely many reviews (>50), the pool could be pre-truncated to the top 30 by rating before sending to Phase 2.

### Scraping throughput

Playwright scraping is ~10-15 seconds per recipe (page load + JS settle). This is the bottleneck. It's independent of the pipeline and can be:
- Pre-run in batch (scrape all recipes first, then run pipeline).
- Parallelised with multiple browser contexts.

### Pipeline throughput

`process_recipe_directory` loops sequentially over recipe files. For a large corpus, this could be parallelised with `concurrent.futures.ThreadPoolExecutor` since each recipe is independent. The LLM calls are already I/O-bound and would benefit from concurrency.

### Confirmed run results

Pipeline v2 was run against all 15 recipe files. **13 out of 15 recipes were successfully enhanced**, producing 13 enhanced JSON files in `data/enhanced/`. The pipeline architecture works — featured tweak preference, multi-modification extraction, safety validation, and Phase 2 summarisation all function correctly.

However, an audit of all 13 enhanced recipes revealed **systemic quality issues** in the LLM output and the modifier's handling of sequential edits.

### Quality audit: systemic issues found across 13 enhanced recipes

| Issue | Recipes affected | Severity |
|-------|-----------------|----------|
| **Instructions not updated to match modified ingredients** | 10 of 13 | Critical |
| **Remove/shadow diffs with empty reasoning** | All 13 | Medium |
| **No-op changes (from_text = to_text)** | 3 (Onion Soup, Cookies, Lasagna) | High |
| **Fabricated modifications not in source review** | 2 (Rice Pudding, Sweet Potato Soup) | Critical |
| **Nonsensical text added as instruction** | 1 (Banana Bread: "Enjoy your Bananarama Bread!") | High |
| **Contradictory reasoning vs actual change** | 1 (Banana Bread: "more bananas" but quantity decreased) | High |
| **Semantically wrong replacement (unrelated items swapped)** | 2 (Crab Soup: water replaced with brown sugar; Nikujaga: index-shift artifacts) | Critical |
| **reviewer: null in source_review** | 6 recipes | Medium |
| **Same review duplicated across all modifications** | 4 recipes | Medium |
| **Misclassified modification_type** | 1 (Apple Cake: "addition" should be "quantity_adjustment") | Low |
| **Copy-pasted reasoning across line_diffs** | 1 (Chinese Chicken Soup) | Low |
| **Vague replacement losing quantity precision** | 2 (Lasagna: 3x "homemade tomato sauce"; Sweet Potato: "Fresh ginger, to taste") | Medium |

### Detailed findings by recipe

**Best results** (2 recipes with minor issues only):
- **Homemade Mac and Cheese**: Cleanest output. Well-sourced modifications, sensible changes. Only issue: 2 remove diffs with empty reasoning.
- **Old-Fashioned Onion Soup**: Mostly clean, but has one no-op change where `from_text` equals `to_text` because the modifier used the already-modified value from a prior edit.

**Critical issues** (most impactful problems):

1. **Instructions never updated when ingredients change** (10 of 13 recipes): When an ingredient is replaced (e.g. "walnuts" → "Skor toffee bits"), the instructions still reference the original ("Stir in flour, chocolate chips, and walnuts"). The pipeline only modifies `ingredients` and `instructions` lists via the LLM's `edits`, but the LLM rarely emits instruction edits to match ingredient changes — and the pipeline doesn't detect or fix the inconsistency.

2. **No-op changes from sequential application** (3 recipes): When multiple modifications target the same ingredient line sequentially, the second modification's `from_text` is the already-modified value, not the original. If the Phase 2 summarisation already resolved them into the same value, the result is `from_text == to_text`. The modifier applies this as a no-op but records it as a successful change.

3. **LLM hallucinations / fabrications** (2 recipes): The LLM occasionally invents modifications not present in the source review. Rice Pudding got "Divide the pudding into two containers" from a review that never said this. Sweet Potato Soup got "Fresh ginger, to taste" from a review about canned yams.

4. **Nonsensical additions** (1 recipe): Banana Bread received "Enjoy your Bananarama Bread!" as an instruction — marketing-style text fabricated by the LLM.

5. **Index-shifting artifacts in line_diffs** (multiple recipes): When `add_after` inserts a new ingredient, all subsequent ingredients shift down by one. The diff builder compares by index, so it reports every shifted line as a "replace" with empty reasoning. These false diffs are confusing for users.

### Root causes

1. **The LLM extraction prompt doesn't instruct for instruction consistency.** When an ingredient changes, the LLM should also emit an instruction edit if the instructions reference that ingredient. The prompt currently only says "extract what the user changed", not "ensure instructions remain consistent".

2. **The modifier applies edits against the evolving recipe, not the original.** Each edit's `find` text is matched against the recipe *after* all prior edits. If two modifications from different reviews change the same line, the second `find` may not match the now-modified text, or may match it as a no-op.

3. **The diff builder compares by index, not by content.** Insertions cause all subsequent lines to shift, producing false diffs. A content-aware diff (e.g. `difflib.SequenceMatcher` on the two lists) would correctly identify the insertion and leave unchanged lines alone.

4. **The Phase 2 summarisation prompt lacks a "no fabrication" instruction.** The LLM should be explicitly told to only use modifications that are directly stated in the source reviews, not inferred or invented.

5. **The `reviewer` field is null when reviews don't have a username** — this is a data issue from `scraper_v2.py` which doesn't extract usernames from the current DOM structure. `scraper_v3.py` does extract them, so this improves as more recipes are scraped via v3.

---

## 11. Logging

Pipeline runs are logged to `logs/pipeline_{timestamp}.log` in the project root. Each log file captures all DEBUG-level and above output from every module. Log format includes timestamp, level, module, function, and line number. Files rotate at 10 MB and auto-clean after 30 days.

### Output directory fix

The pipeline's `output_dir` defaults to `PROJECT_ROOT / "data" / "enhanced"` (anchored via `Path(__file__)`), ensuring output always goes to the project root's `data/enhanced/` regardless of working directory.

---

## 13. Future Improvements

### Critical (from audit findings)

1. **Instruction consistency pass**: After ingredient modifications are applied, run a follow-up LLM call (or post-processing step) that checks whether the instructions reference any replaced/removed ingredient names and updates them accordingly. Currently 10 of 13 recipes have stale instructions that reference ingredients no longer in the recipe.

2. **Content-aware diff builder**: Replace the current index-based diff comparison with `difflib.SequenceMatcher` operating on the two ingredient/instruction lists. This correctly identifies insertions and removals without producing false "replace" diffs from index shifting. Each real diff should carry attribution from the modification that caused it.

3. **No-op change detection**: Before recording a `ChangeRecord`, compare `from_text` and `to_text`. If they are identical, skip the record. This eliminates the 3-recipe pattern where sequential modifications against the same line produce no-ops.

4. **Anti-hallucination guardrail in prompts**: Add explicit instructions to both Phase 1 and Phase 2 prompts: "Only extract modifications that are directly and explicitly stated in the review text. Do not infer, invent, or extrapolate changes the reviewer did not describe." This addresses the fabricated modifications found in Rice Pudding and Sweet Potato Soup.

5. **Post-apply validation**: After all modifications are applied, run a sanity check that verifies: (a) no instruction references an ingredient that was removed, (b) no line_diff has empty reasoning, (c) no added instruction is nonsensical/marketing text. Flag or reject failing recipes.

### High priority

6. **Two-pass `has_modification` detection**: Use the regex as a quick pre-filter, then a lightweight LLM call to confirm. This eliminates false positives like "I used this recipe as-is" without the cost of running full extraction on every review.

7. **Instruction modification improvements**: The fuzzy matching (`SequenceMatcher`) works well for short ingredient lines but is less reliable for long instruction strings. Consider using the LLM to identify the exact instruction text to find, or switch to a more robust matching strategy for instructions.

8. **Proper test suite**: Replace the manual `test_pipeline.py` runner with pytest tests that use mocked LLM responses and assert specific outputs. Include golden-file tests against known-good enhanced recipe JSONs to catch regressions.

### Medium priority

9. **Reviewer attribution fix**: Populate `reviewer` field on all `source_review` entries. Currently 6 recipes have `reviewer: null` because `scraper_v2.py` doesn't extract usernames from the current DOM. For v2 scraped data, backfill from `featured_tweaks` username field where available.

10. **Precision in replacements**: The Phase 2 prompt should instruct the LLM to preserve quantity specificity. "28 oz can crushed tomatoes" should not become the vague "homemade tomato sauce" — either preserve the original or provide a specific substitute with quantities.

11. **Nutrition recalculation**: The enhanced recipe copies the original nutrition data even after ingredient changes. At minimum, flag it as stale. Ideally, recalculate based on modified ingredients.

12. **Confidence scoring**: Add a `confidence_score` field to each modification based on: (a) how many reviewers mention it, (b) average rating of those reviewers, (c) fuzzy match quality when applying.

13. **Deterministic mode**: Set LLM temperature to 0 and seed any randomness for reproducible runs.

### Nice to have

14. **Review pagination**: The scraper could click "Load More" or paginate to capture more reviews.

15. **Cross-recipe learning**: Modifications that work across many similar recipes (e.g. "add vanilla to any cookie recipe") could be identified and suggested proactively.

16. **User feedback loop**: Allow users to vote on whether a modification actually improved their result.

17. **API endpoint**: Wrap the pipeline in a FastAPI service that accepts a recipe URL and returns the enhanced recipe JSON.

---

## 14. File Inventory

### Source files

| File | Lines | Purpose |
|------|-------|---------|
| `src/scraper_v2.py` | 443 | Original scraper (unchanged) — `requests` + BeautifulSoup |
| `src/scraper_v2_initialissues.py` | 611 | Snapshot of scraper during VPN-related issues |
| `src/scraper_v3.py` | 474 | Playwright headless scraper with Featured Tweaks extraction |
| `src/llm_pipeline/models.py` | 219 | v2 Pydantic models |
| `src/llm_pipeline/prompts.py` | 256 | Few-shot extraction + summarisation prompts |
| `src/llm_pipeline/tweak_extractor.py` | 195 | Two-phase LLM extractor |
| `src/llm_pipeline/pipeline.py` | 264 | v2 orchestrator |
| `src/llm_pipeline/enhanced_recipe_generator.py` | 251 | v2 generator with line diffs |
| `src/llm_pipeline/recipe_modifier.py` | 258 | Modifier with safety validation |
| `src/test_pipeline.py` | 161 | Test runner with diff output and assertions |

### Data files

| File | Purpose |
|------|---------|
| `data/recipe_*.json` (16 files) | Scraped recipe data |
| `data/enhanced/enhanced_*.json` (13 files) | Enhanced recipe output from pipeline v2 |
| `data/enhanced/pipeline_summary_report.json` | Summary of batch processing results |
| `data/references/example_featured_tweaks.xml` | Reference HTML for Featured Tweaks carousel |

### Logs

| File | Purpose |
|------|---------|
| `logs/pipeline_{timestamp}.log` | Timestamped pipeline run logs (DEBUG level, all modules) |

### Documentation files

| File | Purpose |
|------|---------|
| `README.md` | Project setup and usage |
| `modifications.md` | Technical log of all changes made |
| `current_state.md` | Pre-v2 pipeline analysis and improvement areas |
| `documentation.md` | This file — comprehensive engineering documentation |
