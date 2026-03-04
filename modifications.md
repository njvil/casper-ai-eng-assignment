# Project Modifications Log

A full record of all changes, investigations, plans, and insights made to this project after the initial commit. `scraper_v2.py` is untouched from its original state. All new functionality lives in new files or targeted pipeline changes.

---

## Files changed

| File | Status |
|------|--------|
| `src/scraper_v2.py` | **Unchanged** — original file, no modifications |
| `src/scraper_v3.py` | **New file** — standalone Playwright-based scraper (renamed from `scraper_playwright.py`) |
| `src/llm_pipeline/models.py` | **Rewritten** — v2 models with `ExtractionResult`, `RankedModification`, `LineDiff`, `BestSource` |
| `src/llm_pipeline/prompts.py` | **Rewritten** — few-shot extraction prompt + Phase 2 summarisation prompt |
| `src/llm_pipeline/tweak_extractor.py` | **Rewritten** — two-phase LLM: extract all → summarise/deduplicate/rank top 5 |
| `src/llm_pipeline/pipeline.py` | **Rewritten** — v2 orchestrator with featured-tweak preference, batch apply, line diffs |
| `src/llm_pipeline/enhanced_recipe_generator.py` | **Rewritten** — accepts multiple modifications, builds line diffs, stores originals |
| `src/llm_pipeline/recipe_modifier.py` | **Modified** — safety validation before each apply, threshold raised to 0.7 |
| `src/test_pipeline.py` | **Updated** — prints line diffs, asserts diffs exist, logs to file |

---

## Output directory and logging fixes

### Output directory anchored to project root

`pipeline.py` originally defaulted `output_dir` to `"data/enhanced"` — a CWD-relative path. When the test runner is executed from `src/`, output went to `src/data/enhanced/` instead of the project root's `data/enhanced/`.

**Fix**: Added `PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent` and `DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "enhanced"` at module level. The constructor now uses this absolute path by default.

### Pipeline log file

`test_pipeline.py` now adds a `loguru` file sink that writes every pipeline run to `logs/pipeline_{timestamp}.log` in the project root. Logs rotate at 10 MB and auto-clean after 30 days. All DEBUG-level output from every module (pipeline, extractor, modifier, generator) is captured.

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

---

## New file: `src/scraper_v3.py`

A fully standalone Playwright-based scraper that replaces the HTTP fetch layer with a headless Chromium browser, making the JS-rendered Featured Tweaks carousel accessible.

### Key design decisions

- **`wait_until="load"`** (not `"networkidle"`): AllRecipes continuously fires background ad/analytics requests so `networkidle` never triggers. `"load"` waits for the browser's `window.load` event which completes reliably.
- **`timeout=60_000`**: explicit 60 s timeout as a safety net.
- **`page.wait_for_timeout(3_000)`**: 3 s settle time for Vue carousels to hydrate.
- **Blocks images/fonts/media** during load to reduce page load time.
- **Saves to `DATA_DIR`** (`project_root/data/`).

### Setup and usage

```bash
uv run playwright install chromium   # one-time
uv run python src/scraper_v3.py
```

---

## Pipeline v2: Two-phase LLM extraction with line-level diffs

### Assumptions

- **Featured tweaks are the primary source.** All reviews in `featured_tweaks` have `has_modification: true` by definition (curated by AllRecipes). The pipeline falls back to base `reviews` only if `featured_tweaks` is empty.
- **One review can contain multiple distinct modifications.** The Phase 1 LLM returns a list of `ModificationObject` items per review, not just one.
- **Same modification across multiple reviews = one modification with higher confidence.** E.g. "use more brown sugar" from 5 reviewers is ranked higher than a unique tweak from 1 reviewer.
- **Conflicting modifications are resolved, not both applied.** If one reviewer says "more sugar" and another says "less sugar" targeting the same ingredient, the Phase 2 LLM keeps the one from a featured tweak, then the one with the higher rating, then higher helpful count.
- **Top 5 unique modifications come from a semantically summarised global list**, not from individual reviews. A Phase 2 LLM call takes the full pool and returns the deduplicated, conflict-resolved, ranked top 5.
- **The enhanced JSON includes original recipe lines alongside modified lines** so a user can see line-level diffs inline.
- **The pipeline is recipe-agnostic** — works on any recipe JSON, not just the cookie recipe.

### Architecture

```
Phase 1 (N LLM calls — one per review):
  For each review with has_modification=True:
    → LLM returns ExtractionResult { modifications: [ModificationObject, ...] }
    → Tag each mod with source review metadata (username, rating, helpful_count, is_featured)
    → Append all to flat global pool

Phase 2 (1 LLM call — summarisation):
  Send entire pool to LLM with build_summarize_prompt()
    → LLM merges semantically identical mods, resolves conflicts, ranks
    → Returns ranked_modifications (at most 5)

Apply:
  For each ranked modification:
    → validate_modification_safety() — skip if unsafe
    → apply_modification() via fuzzy string matching
  Build line_diffs by comparing original vs modified recipe line by line
  Generate EnhancedRecipe with full attribution
```

### Prompt changes

- **Phase 1**: Switched from `build_simple_prompt` to `build_few_shot_prompt`. The prompt now requests `{"modifications": [...]}` so the LLM can return multiple distinct modifications from a single review. Includes explicit instruction: "If the review contains multiple distinct modifications, return each as a separate object."
- **Phase 2**: New `build_summarize_prompt` function. Receives the full pool with source metadata. Instructs the LLM to merge duplicates, resolve conflicts using priority rules (featured > rating > helpful_count), and return at most 5 ranked modifications.

### Model changes

| Model | Change |
|-------|--------|
| `ModificationObject` | `modification_type` remains a single `Literal` per object |
| `ExtractionResult` | **New** — wraps `List[ModificationObject]` for Phase 1 response |
| `BestSource` | **New** — username, rating, helpful_count, is_featured |
| `RankedModification` | **New** — extends ModificationObject with `modification_type: List[...]`, `mention_count`, `best_source` |
| `LineDiff` | **New** — section, line_index, original, modified, operation, source_username, reasoning |
| `Review` | Added `helpful_count`, `feedback_id`, `is_featured` fields |
| `EnhancedRecipe` | Added `original_ingredients`, `original_instructions`, `line_diffs` |
| `ModificationApplied` | `modification_type` is now `List[str]`, added `mention_count` |
| `EnhancementSummary` | Unchanged |

### Pipeline changes

- `parse_reviews_data` now **prefers `featured_tweaks`** over `reviews`. If featured tweaks are present, only those are used. Falls back to base reviews otherwise.
- `process_single_recipe` calls `extract_all_modifications` (two-phase) instead of `extract_single_modification` (random pick).
- `apply_modifications_batch` now calls `validate_modification_safety` before each modification. Unsafe modifications are skipped with a warning instead of silently failing at the fuzzy-match level.
- Similarity threshold raised from 0.6 to 0.7 to reduce false matches.
- Pipeline version bumped to `"2.0.0"`.

### Enhanced recipe output schema

```json
{
  "recipe_id": "10813_enhanced",
  "original_recipe_id": "10813",
  "title": "Best Chocolate Chip Cookies (Community Enhanced)",
  "original_ingredients": ["1 cup butter, softened", "1 cup white sugar", ...],
  "ingredients": ["1 cup butter, softened", "0.5 cup white sugar", ...],
  "original_instructions": ["Preheat the oven to 350...", ...],
  "instructions": ["Preheat the oven to 350...", ...],
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
      "source_review": { "text": "...", "reviewer": "Jen Gerbrandt", "rating": 5 },
      "modification_type": ["quantity_adjustment"],
      "reasoning": "...",
      "changes_made": [...],
      "mention_count": 3
    }
  ],
  "enhancement_summary": {
    "total_changes": 3,
    "change_types": ["quantity_adjustment", "removal"],
    "expected_impact": "..."
  },
  "pipeline_version": "2.0.0"
}
```

The `line_diffs` array is the primary data a UI uses to render an inline diff view showing exactly what changed, who suggested it, and why.

### Scalability

- **LLM calls per recipe**: N (one per review, typically 5-15) + 1 (summarisation) = ~6-16 calls. At ~$0.001/call with gpt-3.5-turbo, this is ~$0.01-0.02/recipe.
- **Summarisation call**: receives the full pool as input. With 15 reviews x ~3 mods each = ~45 modification objects, this fits well within gpt-3.5-turbo's context window.
- **Playwright scraping**: ~10-15s per recipe, independent of the pipeline.
- **Batch processing**: `process_recipe_directory` loops sequentially. Could be parallelised with `concurrent.futures` if needed.

### Confirmed run results

Pipeline v2 was run against all 15 recipe files. **13 out of 15 recipes were successfully enhanced**, producing 13 enhanced JSON files in `data/enhanced/`. The pipeline correctly:
- Used featured tweaks as primary source when available (e.g. Italian Wedding Soup with 11 featured tweaks).
- Fell back to base reviews when no featured tweaks existed (e.g. Banana Bread).
- Extracted multiple modifications per review (e.g. 5 modifications from a single review of Italian Wedding Soup).
- Skipped unsafe modifications with logged warnings (e.g. `find` text not matching any recipe line).
- Generated line-level diffs for all applied changes.
- Saved all output to `data/enhanced/` and full logs to `logs/`.
