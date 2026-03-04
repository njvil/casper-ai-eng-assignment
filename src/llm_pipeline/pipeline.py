"""
LLM Analysis Pipeline v2 - Main Orchestrator

Two-phase pipeline:
  Phase 1: Extract modifications from every review (prefer featured_tweaks)
  Phase 2: Semantically deduplicate, resolve conflicts, rank top 5
  Apply:   Validate safety, apply sequentially, build line diffs, save

Processes recipe data from scraped JSON files and outputs enhanced recipes.
"""

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from loguru import logger

from .enhanced_recipe_generator import EnhancedRecipeGenerator
from .models import (
    ChangeRecord,
    EnhancedRecipe,
    ModificationObject,
    RankedModification,
    Recipe,
    Review,
)
from .recipe_modifier import RecipeModifier
from .tweak_extractor import TweakExtractor


class LLMAnalysisPipeline:
    """Complete pipeline for analyzing recipes and generating enhanced versions."""

    def __init__(
        self,
        openai_api_key: Optional[str] = None,
        output_dir: str = "data/enhanced",
        pipeline_version: str = "2.0.0",
    ):
        load_dotenv()

        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.tweak_extractor = TweakExtractor(api_key=openai_api_key)
        self.recipe_modifier = RecipeModifier()
        self.enhanced_generator = EnhancedRecipeGenerator(
            pipeline_version=pipeline_version
        )

        logger.info(f"Initialized LLM Analysis Pipeline v{pipeline_version}")
        logger.info(f"Output directory: {self.output_dir}")

    # ------------------------------------------------------------------
    # Data loading / parsing
    # ------------------------------------------------------------------

    def load_recipe_data(self, file_path: str) -> Dict[str, Any]:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def parse_recipe_data(self, recipe_data: Dict[str, Any]) -> Recipe:
        return Recipe(
            recipe_id=recipe_data.get("recipe_id", "unknown"),
            title=recipe_data.get("title", "Unknown Recipe"),
            ingredients=recipe_data.get("ingredients", []),
            instructions=recipe_data.get("instructions", []),
            description=recipe_data.get("description"),
            servings=recipe_data.get("servings"),
            rating=recipe_data.get("rating"),
        )

    def parse_reviews_data(self, recipe_data: Dict[str, Any]) -> List[Review]:
        """Parse reviews, preferring featured_tweaks; fall back to base reviews."""
        reviews: List[Review] = []
        seen_ids: set = set()

        featured = recipe_data.get("featured_tweaks", [])
        use_featured = len(featured) > 0

        if use_featured:
            logger.info(
                f"Using {len(featured)} featured tweaks as primary source"
            )
            for tweak in featured:
                if not tweak.get("text"):
                    continue
                fid = tweak.get("feedback_id") or tweak["text"]
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
                reviews.append(
                    Review(
                        text=tweak["text"],
                        rating=tweak.get("rating"),
                        username=tweak.get("username"),
                        has_modification=True,
                        helpful_count=tweak.get("helpful_count"),
                        feedback_id=tweak.get("feedback_id"),
                        is_featured=True,
                    )
                )
        else:
            logger.info("No featured tweaks found, falling back to base reviews")
            for review_data in recipe_data.get("reviews", []):
                if review_data.get("text"):
                    reviews.append(
                        Review(
                            text=review_data["text"],
                            rating=review_data.get("rating"),
                            username=review_data.get("username"),
                            has_modification=review_data.get(
                                "has_modification", False
                            ),
                            helpful_count=review_data.get("helpful_count"),
                            feedback_id=review_data.get("feedback_id"),
                            is_featured=False,
                        )
                    )

        return reviews

    # ------------------------------------------------------------------
    # Core pipeline
    # ------------------------------------------------------------------

    def process_single_recipe(
        self, recipe_file: str, save_output: bool = True
    ) -> Optional[EnhancedRecipe]:
        try:
            logger.info(f"Processing recipe file: {recipe_file}")

            # Step 0: Load and parse
            recipe_data = self.load_recipe_data(recipe_file)
            recipe = self.parse_recipe_data(recipe_data)
            reviews = self.parse_reviews_data(recipe_data)

            mod_reviews = [r for r in reviews if r.has_modification]
            logger.info(
                f"Loaded recipe: {recipe.title} — "
                f"{len(reviews)} reviews, {len(mod_reviews)} with modifications"
            )

            if not mod_reviews:
                logger.warning("No reviews with modifications found")
                return None

            # Step 1+2: Two-phase extraction → ranked top 5
            logger.info("Step 1+2: Extracting & ranking modifications...")
            ranked_pairs = self.tweak_extractor.extract_all_modifications(
                reviews, recipe
            )

            if not ranked_pairs:
                logger.warning("No modifications could be extracted")
                return None

            logger.info(
                f"Got {len(ranked_pairs)} ranked modifications after deduplication"
            )

            # Step 3+4: Convert RankedModifications to ModificationObjects and
            # apply sequentially (safety validation happens inside batch)
            mod_objects: List[ModificationObject] = []
            ranked_mods_ordered: List[Tuple[RankedModification, Review]] = []

            for rm, review in ranked_pairs:
                mod_obj = ModificationObject(
                    modification_type=rm.modification_type[0]
                    if rm.modification_type
                    else "addition",
                    reasoning=rm.reasoning,
                    edits=rm.edits,
                )
                mod_objects.append(mod_obj)
                ranked_mods_ordered.append((rm, review))

            logger.info("Step 3+4: Applying modifications with safety checks...")
            modified_recipe, change_records_per_mod = (
                self.recipe_modifier.apply_modifications_batch(recipe, mod_objects)
            )

            total_changes = sum(len(cr) for cr in change_records_per_mod)
            logger.info(f"Applied modifications: {total_changes} total changes made")

            # Build (RankedMod, Review, ChangeRecords) triples
            mod_review_changes = [
                (rm, review, records)
                for (rm, review), records in zip(
                    ranked_mods_ordered, change_records_per_mod
                )
                if records  # skip modifications that produced no changes
            ]

            if not mod_review_changes:
                logger.warning("No modifications produced any changes")
                return None

            # Step 5: Build line diffs
            logger.info("Step 5: Building line-level diffs...")
            line_diffs = EnhancedRecipeGenerator.build_line_diffs(
                recipe, modified_recipe, mod_review_changes
            )
            logger.info(f"Generated {len(line_diffs)} line diffs")

            # Step 6: Generate enhanced recipe
            logger.info("Step 6: Generating enhanced recipe with attribution...")
            enhanced_recipe = self.enhanced_generator.generate_enhanced_recipe(
                recipe, modified_recipe, mod_review_changes, line_diffs
            )
            logger.info(f"Generated enhanced recipe: {enhanced_recipe.title}")

            # Step 7: Save
            if save_output:
                slug = recipe.title.lower().replace(" ", "-")[:30]
                output_filename = f"enhanced_{recipe.recipe_id}_{slug}.json"
                output_path = self.output_dir / output_filename
                self.enhanced_generator.save_enhanced_recipe(
                    enhanced_recipe, str(output_path)
                )

            return enhanced_recipe

        except Exception as e:
            logger.error(f"Failed to process recipe {recipe_file}: {e}")
            import traceback

            traceback.print_exc()
            return None

    # ------------------------------------------------------------------
    # Batch processing
    # ------------------------------------------------------------------

    def process_recipe_directory(
        self, data_dir: str = "data"
    ) -> List[EnhancedRecipe]:
        data_path = Path(data_dir)
        recipe_files = list(data_path.glob("recipe_*.json"))

        logger.info(f"Found {len(recipe_files)} recipe files to process")

        enhanced_recipes = []
        for recipe_file in recipe_files:
            logger.info(f"\n{'=' * 60}")
            enhanced = self.process_single_recipe(str(recipe_file))
            if enhanced:
                enhanced_recipes.append(enhanced)
                logger.info(f"✓ Successfully processed: {enhanced.title}")
            else:
                logger.warning(f"✗ Failed to process: {recipe_file.name}")

        logger.info(f"\n{'=' * 60}")
        logger.info(
            f"Pipeline complete: {len(enhanced_recipes)}/{len(recipe_files)} "
            f"recipes successfully enhanced"
        )
        return enhanced_recipes

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def generate_summary_report(
        self, enhanced_recipes: List[EnhancedRecipe]
    ) -> Dict[str, Any]:
        if not enhanced_recipes:
            return {"status": "no_recipes_processed"}

        total_modifications = sum(
            len(r.modifications_applied) for r in enhanced_recipes
        )
        total_changes = sum(
            r.enhancement_summary.total_changes for r in enhanced_recipes
        )
        total_diffs = sum(len(r.line_diffs) for r in enhanced_recipes)

        change_type_counts: Dict[str, int] = {}
        for recipe in enhanced_recipes:
            for ct in recipe.enhancement_summary.change_types:
                change_type_counts[ct] = change_type_counts.get(ct, 0) + 1

        return {
            "pipeline_version": "2.0.0",
            "pipeline_summary": {
                "recipes_processed": len(enhanced_recipes),
                "total_modifications_applied": total_modifications,
                "total_changes_made": total_changes,
                "total_line_diffs": total_diffs,
                "change_type_distribution": change_type_counts,
            },
            "enhanced_recipes": [
                {
                    "recipe_id": r.recipe_id,
                    "title": r.title,
                    "modifications_count": len(r.modifications_applied),
                    "changes_count": r.enhancement_summary.total_changes,
                    "line_diffs_count": len(r.line_diffs),
                    "change_types": r.enhancement_summary.change_types,
                }
                for r in enhanced_recipes
            ],
        }

    def save_summary_report(
        self,
        enhanced_recipes: List[EnhancedRecipe],
        output_path: Optional[str] = None,
    ) -> str:
        if output_path is None:
            output_path = str(self.output_dir / "pipeline_summary_report.json")

        report = self.generate_summary_report(enhanced_recipes)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        logger.info(f"Saved pipeline summary report to: {output_path}")
        return output_path
