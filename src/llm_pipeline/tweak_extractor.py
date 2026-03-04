"""
Step 1: Tweak Extraction & Ranking (v2 — two-phase LLM)

Phase 1: Extract structured modifications from every review via individual LLM calls.
Phase 2: Send the full pool to one LLM call that semantically deduplicates,
         resolves conflicts, and returns the ranked top 5.
"""

import json
import os
from typing import Dict, List, Optional, Tuple

from loguru import logger
from openai import OpenAI
from pydantic import ValidationError

from .models import (
    ExtractionResult,
    ModificationObject,
    RankedModification,
    Recipe,
    Review,
)
from .prompts import build_few_shot_prompt, build_summarize_prompt


class TweakExtractor:
    """Extracts structured modifications from review text using LLM processing."""

    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-3.5-turbo"):
        self.client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
        self.model = model
        logger.info(f"Initialized TweakExtractor with model: {model}")

    # ------------------------------------------------------------------
    # Phase 1 — per-review extraction
    # ------------------------------------------------------------------

    def extract_modification(
        self,
        review: Review,
        recipe: Recipe,
        max_retries: int = 2,
    ) -> List[ModificationObject]:
        """Extract structured modifications from a single review.

        Returns a list because one review can contain multiple distinct
        modifications (e.g. change sugar AND remove nuts).
        """
        if not review.has_modification:
            return []

        prompt = build_few_shot_prompt(
            review.text, recipe.title, recipe.ingredients, recipe.instructions
        )

        logger.debug(f"Extracting from review: {review.text[:100]}...")

        for attempt in range(max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                    temperature=0.1,
                    max_tokens=2000,
                )

                raw_output = response.choices[0].message.content
                if not raw_output:
                    logger.warning(f"Attempt {attempt + 1}: Empty LLM response")
                    continue

                data = json.loads(raw_output)

                # Handle both old single-object and new list format
                if "modifications" in data:
                    result = ExtractionResult(**data)
                    mods = result.modifications
                else:
                    mods = [ModificationObject(**data)]

                logger.info(
                    f"Extracted {len(mods)} modification(s) from review"
                )
                return mods

            except (json.JSONDecodeError, ValidationError) as e:
                logger.warning(f"Attempt {attempt + 1}: Parse/validation error: {e}")
            except Exception as e:
                logger.error(f"Attempt {attempt + 1}: Unexpected error: {e}")

        return []

    # ------------------------------------------------------------------
    # Phase 2 — summarise, deduplicate, resolve conflicts
    # ------------------------------------------------------------------

    def _build_pool_json(
        self,
        pool: List[Dict],
    ) -> str:
        """Serialise the raw modification pool for the summarisation prompt."""
        return json.dumps(pool, indent=2, ensure_ascii=False)

    def summarize_modifications(
        self,
        pool: List[Dict],
        recipe: Recipe,
        max_retries: int = 2,
    ) -> List[RankedModification]:
        """Phase 2: Send the full pool to the LLM for semantic deduplication,
        conflict resolution, and ranking. Returns at most 5 items."""

        pool_json = self._build_pool_json(pool)
        prompt = build_summarize_prompt(
            recipe.title, recipe.ingredients, recipe.instructions, pool_json
        )

        logger.info(f"Phase 2: summarising {len(pool)} raw modifications")

        for attempt in range(max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                    temperature=0.1,
                    max_tokens=3000,
                )

                raw_output = response.choices[0].message.content
                if not raw_output:
                    logger.warning(f"Attempt {attempt + 1}: Empty LLM response")
                    continue

                data = json.loads(raw_output)
                ranked = [
                    RankedModification(**item)
                    for item in data.get("ranked_modifications", [])
                ]

                logger.info(f"Phase 2 returned {len(ranked)} ranked modifications")
                return ranked[:5]

            except (json.JSONDecodeError, ValidationError) as e:
                logger.warning(
                    f"Phase 2 attempt {attempt + 1}: Parse/validation error: {e}"
                )
            except Exception as e:
                logger.error(f"Phase 2 attempt {attempt + 1}: Unexpected error: {e}")

        return []

    # ------------------------------------------------------------------
    # Public API — full two-phase pipeline
    # ------------------------------------------------------------------

    def extract_all_modifications(
        self,
        reviews: List[Review],
        recipe: Recipe,
    ) -> List[Tuple[RankedModification, Review]]:
        """Run the full two-phase extraction pipeline.

        1. Extract modifications from every review into a flat pool.
        2. Summarise the pool into a ranked top 5.

        Returns a list of (RankedModification, best_source_Review) tuples.
        """

        modification_reviews = [r for r in reviews if r.has_modification]
        if not modification_reviews:
            logger.warning("No reviews with modifications found")
            return []

        # Phase 1: extract from each review
        pool: List[Dict] = []
        review_lookup: Dict[str, Review] = {}

        for review in modification_reviews:
            mods = self.extract_modification(review, recipe)
            for mod in mods:
                entry = mod.model_dump()
                entry["source"] = {
                    "username": review.username,
                    "rating": review.rating,
                    "helpful_count": review.helpful_count,
                    "is_featured": review.is_featured,
                }
                pool.append(entry)

                key = review.username or review.text[:50]
                review_lookup[key] = review

        if not pool:
            logger.warning("Phase 1 produced no modifications")
            return []

        logger.info(f"Phase 1 complete: {len(pool)} raw modifications from {len(modification_reviews)} reviews")

        # Phase 2: summarise / deduplicate / rank
        ranked = self.summarize_modifications(pool, recipe)
        if not ranked:
            logger.warning("Phase 2 returned no ranked modifications")
            return []

        # Pair each ranked modification with its best source review
        results: List[Tuple[RankedModification, Review]] = []
        for rm in ranked:
            username = rm.best_source.username
            matched_review = review_lookup.get(username or "")
            if not matched_review:
                matched_review = modification_reviews[0]
            results.append((rm, matched_review))

        return results

    # ------------------------------------------------------------------
    # Backward-compatible helpers
    # ------------------------------------------------------------------

    def extract_single_modification(
        self, reviews: List[Review], recipe: Recipe
    ) -> Tuple[Optional[ModificationObject], Optional[Review]]:
        """Legacy API — extracts from one random review. Kept for testing."""
        import random

        modification_reviews = [r for r in reviews if r.has_modification]
        if not modification_reviews:
            return None, None

        selected = random.choice(modification_reviews)
        mods = self.extract_modification(selected, recipe)
        if mods:
            return mods[0], selected
        return None, None
