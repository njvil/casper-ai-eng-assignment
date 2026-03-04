# Current State of the Pipeline (Pre-v2 Analysis)

This document was written as a pre-v2 analysis of the original pipeline. It identifies the assumptions, limitations, and areas for improvement that informed the v2 redesign. See `modifications.md` for what was changed and `documentation.md` for the full engineering narrative.

**Note**: Many of the issues listed below have been addressed in pipeline v2: multi-review processing, few-shot prompting, safety validation, featured tweak preference, line-level diffs, output directory fix, and log file support.

---

## How the base recipe became the enhanced recipe

Examining `data/recipe_10813_best-chocolate-chip-cookies.json` → `data/enhanced/enhanced_10813_best-chocolate-chip-cookies.json`:

### What the enhanced output contains

Two `modifications_applied` entries:

1. **Review**: "I used an ice cream scoop, that made 16 big cookies. I did add an additional egg yolk..."
   - Type: `addition`
   - Change: Added `"1 additional egg yolk"` to ingredients
   - The "ice cream scoop" tip was ignored (technique, not ingredient — good behaviour)

2. **Review**: "These are awesome cookies. I followed the advice of others by making the following tweaks: (1) sugar ratios (2) omitted water (3) cream of tartar (4) refrigerated batter..."
   - Type: `quantity_adjustment`
   - Changes: White sugar `1 cup → 0.5 cup`, brown sugar `1 cup → 1.5 cups`
   - **4 modifications existed in this review, only 2 were extracted** — the water removal, cream of tartar addition, and refrigeration technique were all dropped

### Discrepancy: the current pipeline can only produce 1 modification per run

`process_single_recipe` (pipeline.py line 166-169):
```python
modification, source_review = self.tweak_extractor.extract_single_modification(reviews, recipe)
```

This picks **one random review** and extracts **one `ModificationObject`** from it. `generate_enhanced_recipe` then wraps it in `modifications_applied = [modification_applied]` — a list with exactly **one** entry.

The enhanced JSON has **two** entries from **two** different reviews. This means the current enhanced output was either:
- Hand-crafted / manually curated as a sample
- Produced by an earlier version of the pipeline that processed multiple reviews

The `confidence_score` field in the enhanced JSON also doesn't exist in the current `ModificationApplied` or `EnhancementSummary` Pydantic models, confirming it was not produced by the code as written.

---

## How the pipeline works (step by step)

```
1. Load JSON file
2. Parse recipe data → Recipe object
3. Parse reviews → List[Review]  (now also includes featured_tweaks)
4. Filter to reviews where has_modification == True
5. Pick ONE random review from that set
6. Send review text + recipe to LLM → ModificationObject (single type + list of edits)
7. Apply edits to recipe via fuzzy string matching → modified Recipe + ChangeRecords
8. Package into EnhancedRecipe with attribution
9. Save to data/enhanced/
```

---

## Assumptions

### 1. One review per run is sufficient
The pipeline picks a single random review and applies its modifications. It assumes that any one review contains enough signal to meaningfully improve a recipe.

### 2. `has_modification` regex is accurate
The scraper flags reviews using 5 regex patterns:
```
I (added|used|substituted|replaced|made with|changed)
(instead of|rather than|in place of)
(next time|will make again|definitely make)
(doubled|tripled|halved|increased|decreased)
(more|less|extra) [\w\s]+
```
**False positives**: "I used this recipe as-is" matches `I used`. "will make again" matches even if no modification was described. "more or less followed the recipe" matches `more ... `.

**False negatives**: Reviews describing modifications without these exact verb patterns get `has_modification: False` and are permanently excluded.

### 3. Each review maps to one modification type
`ModificationObject.modification_type` is a single enum value. The LLM must pick one of: `ingredient_substitution`, `quantity_adjustment`, `technique_change`, `addition`, `removal`. Reviews that contain multiple types of changes (e.g. substitution AND addition AND technique change) are forced into a single category, and the LLM typically picks the dominant one and drops the rest.

### 4. Fuzzy matching at 0.6 threshold is safe
`RecipeModifier` uses `SequenceMatcher` with a 0.6 threshold to find ingredient/instruction strings. This is low enough that a `find` string from the LLM could match the wrong line in the recipe.

### 5. The LLM output is structurally valid
The pipeline retries up to 2 times on JSON parse errors or Pydantic validation failures, but does no semantic validation — e.g. it doesn't check whether the `find` text actually exists in the recipe before sending it to the modifier.

### 6. Random selection is representative
`random.choice(modification_reviews)` gives equal probability to every flagged review regardless of quality signals like star rating, helpful count, or how many concrete modifications it contains.

### 7. Server-rendered HTML is sufficient for scraping
`scraper_v2.py` uses `requests.get`. The Featured Tweaks carousel is JS-rendered and never appears. This is why `scraper_v3.py` was created — but `scraper_v2.py` still operates on the assumption that relevant data is in the initial HTML.

---

## Areas for improvement

### Critical

**1. Process multiple reviews, not just one**
The pipeline should extract modifications from multiple high-quality reviews (or all of them) and merge them intelligently, rather than randomly picking one. The `apply_modifications_batch` method already exists in `RecipeModifier` but is never called.

**2. Multi-type modification extraction per review**
A single review like "(1) changed sugar ratios (2) omitted water (3) added cream of tartar (4) refrigerated batter" contains 4 distinct modifications. The LLM should be allowed to return multiple `ModificationObject` items, or the schema should support a list of `modification_type` values. Currently the second review's water removal, cream of tartar addition, and refrigeration technique were all silently dropped.

**3. Aggregate recurring modifications across reviews**
Many reviews for the same recipe suggest identical changes (e.g. dozens of people say "use more brown sugar"). The pipeline should identify and weight these recurring patterns rather than treating each review independently. A modification mentioned by 50 reviewers is more trustworthy than one mentioned by 1.

### Important

**4. Smarter review selection**
Instead of `random.choice`, rank reviews by:
- Star rating (prefer 4-5 stars — successful modifications)
- Helpful count (community-validated usefulness)
- Number of distinct modifications detected
- Specificity of language (concrete measurements vs. vague descriptions)

**5. Fix `has_modification` false positives**
"I used this recipe" / "will make again" / "a little more or less" all trigger the flag without describing any actual modification. Consider:
- Two-pass detection: regex to flag candidates, then a lightweight LLM call to confirm
- Negative patterns: exclude phrases like "used this recipe as-is", "followed exactly"
- Or simply remove the regex filter entirely and let the LLM decide if a review contains modifications (more expensive but more accurate)

**6. Pre-validate LLM `find` strings before applying**
`RecipeModifier.apply_edit` silently warns and skips when fuzzy matching fails. The pipeline should call `validate_modification_safety` (which already exists but is never used) before applying, and optionally re-prompt the LLM with the actual ingredient/instruction list if the first extraction has bad `find` values.

**7. Use few-shot prompting**
`build_few_shot_prompt` exists in `prompts.py` with 4 well-crafted examples. But `tweak_extractor.py` line 58 calls `build_simple_prompt` instead. The few-shot examples directly address the most common extraction patterns (sugar ratio, cream of tartar, salt adjustment, temperature change) and would improve extraction quality.

### Nice to have

**8. Conflict detection**
If multiple reviews are processed, their modifications may conflict (one says "more salt", another says "less salt"). The pipeline should detect and resolve these — e.g. by majority vote or by favouring higher-rated reviews.

**9. Confidence scoring**
The enhanced JSON sample includes `confidence_score` but the current models don't define it. Re-adding it as a pipeline feature (based on LLM self-assessment, fuzzy match scores, and reviewer credibility) would help downstream consumers decide which modifications to trust.

**10. Instruction modifications are underserved**
Almost all few-shot examples and the actual enhanced output focus on ingredient changes. Technique changes (temperature, timing, method) are equally valuable but the pipeline's `find` text matching is less reliable for long instruction strings than for short ingredient lines.

**11. Nutrition recalculation**
The enhanced recipe keeps the original nutrition data even though ingredients changed. For accuracy, nutrition should be flagged as stale or recalculated.

**12. Deduplication between `reviews` and `featured_tweaks`**
In the original scraped data, the same review text appears in both `reviews` and `featured_tweaks` (4 reviews are duplicated in the chocolate chip cookie JSON). The pipeline's `parse_reviews_data` deduplicates featured tweaks against each other via `feedback_id`, but doesn't deduplicate against the `reviews` list because `reviews` entries lack a `feedback_id`. This could lead to the same review being processed twice.

**13. `test_pipeline.py` is not a test suite**
It's a manual runner with `single` and `all` modes, but has no assertions, no expected output comparison, and no way to detect regressions. A proper test suite with mocked LLM responses and expected outputs would catch breakage.

**14. Non-deterministic results**
Between `random.choice` for review selection and LLM temperature at 0.1 (not 0), every pipeline run can produce different results for the same input. For reproducibility, consider seeding the random selection and/or caching LLM responses.

---

## Pipeline data flow diagram

```
scraper_v2.py (requests)          scraper_v3.py (headless browser)
         │                                       │
         ▼                                       ▼
    data/*.json ◄────────────────────────────────┘
         │
         ▼
  pipeline.py  ──►  parse_reviews_data()
         │               │
         │          reads "reviews" + "featured_tweaks"
         │               │
         ▼               ▼
  tweak_extractor.py     List[Review]
         │                   │
         │          random.choice(has_modification=True)
         │                   │
         ▼                   ▼
  OpenAI LLM (gpt-3.5-turbo)
         │
         ▼
  ModificationObject  (1 type, N edits)
         │
         ▼
  recipe_modifier.py  ──►  fuzzy match + apply edits
         │
         ▼
  enhanced_recipe_generator.py  ──►  package with attribution
         │
         ▼
  data/enhanced/enhanced_*.json
```

---

## Summary table

| Area | Current behaviour | Gap |
|------|------------------|-----|
| Reviews per run | 1 random | Should process multiple, aggregate |
| Mods per review | 1 type forced | Should allow multi-type extraction |
| Review selection | `random.choice` | Should rank by rating/helpfulness |
| `has_modification` | 5 regex patterns | High false-positive rate |
| LLM prompt | Simple (no examples) | Few-shot examples exist but unused |
| Safety validation | Exists, never called | Should be called before applying |
| Featured tweaks | Scraped via Playwright, fed to pipeline | Working as intended |
| Nutrition | Copied verbatim | Should flag as stale after changes |
| Testing | Manual runner | No assertions or regression detection |
| Determinism | Random + LLM variance | Non-reproducible runs |
