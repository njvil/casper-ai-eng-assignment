"""
Step 3: Enhanced Recipe Generation with Attribution (v2)

Generates enhanced recipes with full citation tracking and line-level diffs.
Accepts multiple modifications from the two-phase extraction pipeline.
"""

from datetime import datetime
from typing import Any, Dict, List, Tuple

from loguru import logger

from .models import (
    ChangeRecord,
    EnhancedRecipe,
    EnhancementSummary,
    LineDiff,
    ModificationApplied,
    RankedModification,
    Recipe,
    Review,
    SourceReview,
)


class EnhancedRecipeGenerator:
    """Generates enhanced recipes with full citation tracking and attribution."""

    def __init__(self, pipeline_version: str = "2.0.0"):
        self.pipeline_version = pipeline_version
        logger.info(f"Initialized EnhancedRecipeGenerator v{pipeline_version}")

    def create_source_review(self, review: Review) -> SourceReview:
        return SourceReview(
            text=review.text, reviewer=review.username, rating=review.rating
        )

    def create_modification_applied(
        self,
        ranked_mod: RankedModification,
        source_review: Review,
        change_records: List[ChangeRecord],
    ) -> ModificationApplied:
        return ModificationApplied(
            source_review=self.create_source_review(source_review),
            modification_type=ranked_mod.modification_type,
            reasoning=ranked_mod.reasoning,
            changes_made=change_records,
            mention_count=ranked_mod.mention_count,
        )

    def calculate_enhancement_summary(
        self, modifications_applied: List[ModificationApplied]
    ) -> EnhancementSummary:
        total_changes = sum(len(mod.changes_made) for mod in modifications_applied)
        change_types = list(
            {t for mod in modifications_applied for t in mod.modification_type}
        )

        impact_descriptions = [
            mod.reasoning for mod in modifications_applied if mod.reasoning
        ]
        expected_impact = "; ".join(impact_descriptions[:3])
        if len(impact_descriptions) > 3:
            expected_impact += (
                f" (and {len(impact_descriptions) - 3} more improvements)"
            )

        return EnhancementSummary(
            total_changes=total_changes,
            change_types=change_types,
            expected_impact=expected_impact
            or "Community-validated recipe improvements",
        )

    @staticmethod
    def build_line_diffs(
        original_recipe: Recipe,
        modified_recipe: Recipe,
        mod_review_changes: List[
            Tuple[RankedModification, Review, List[ChangeRecord]]
        ],
    ) -> List[LineDiff]:
        """Compare original vs modified recipe element-wise and emit LineDiffs.

        For each ChangeRecord produced during apply, we emit a LineDiff with
        the source attribution from the modification that caused it.
        """
        diffs: List[LineDiff] = []

        change_to_source: Dict[str, Tuple[str, str]] = {}
        for rm, review, records in mod_review_changes:
            for cr in records:
                key = f"{cr.type}:{cr.from_text}:{cr.to_text}"
                change_to_source[key] = (
                    review.username or rm.best_source.username or "",
                    rm.reasoning,
                )

        for section_name, orig_list, mod_list in [
            ("ingredients", original_recipe.ingredients, modified_recipe.ingredients),
            ("instructions", original_recipe.instructions, modified_recipe.instructions),
        ]:
            cr_type = "ingredient" if section_name == "ingredients" else "instruction"

            orig_set = set(orig_list)
            mod_set = set(mod_list)

            # Replacements: same index, different text
            max_shared = min(len(orig_list), len(mod_list))
            for i in range(max_shared):
                if orig_list[i] != mod_list[i]:
                    key = f"{cr_type}:{orig_list[i]}:{mod_list[i]}"
                    username, reasoning = change_to_source.get(key, ("", ""))
                    diffs.append(
                        LineDiff(
                            section=section_name,
                            line_index=i,
                            original=orig_list[i],
                            modified=mod_list[i],
                            operation="replace",
                            source_username=username or None,
                            reasoning=reasoning,
                        )
                    )

            # Additions: lines in modified but not in original
            for i, line in enumerate(mod_list):
                if line not in orig_set:
                    already_covered = any(
                        d.modified == line and d.section == section_name
                        for d in diffs
                    )
                    if not already_covered:
                        key = f"{cr_type}::{line}"
                        username, reasoning = change_to_source.get(key, ("", ""))
                        diffs.append(
                            LineDiff(
                                section=section_name,
                                line_index=i,
                                original="",
                                modified=line,
                                operation="add",
                                source_username=username or None,
                                reasoning=reasoning,
                            )
                        )

            # Removals: lines in original but not in modified
            for i, line in enumerate(orig_list):
                if line not in mod_set:
                    key = f"{cr_type}:{line}:"
                    username, reasoning = change_to_source.get(key, ("", ""))
                    diffs.append(
                        LineDiff(
                            section=section_name,
                            line_index=i,
                            original=line,
                            modified="",
                            operation="remove",
                            source_username=username or None,
                            reasoning=reasoning,
                        )
                    )

        return diffs

    def generate_enhanced_recipe(
        self,
        original_recipe: Recipe,
        modified_recipe: Recipe,
        mod_review_changes: List[
            Tuple[RankedModification, Review, List[ChangeRecord]]
        ],
        line_diffs: List[LineDiff],
    ) -> EnhancedRecipe:
        """Generate a complete enhanced recipe with attribution from multiple
        modifications.

        Args:
            original_recipe: Original unmodified recipe
            modified_recipe: Recipe with all modifications applied
            mod_review_changes: List of (RankedModification, source Review,
                                         ChangeRecords) for each applied mod
            line_diffs: Pre-computed line-level diffs
        """
        logger.info(f"Generating enhanced recipe for: {original_recipe.title}")

        modifications_applied = [
            self.create_modification_applied(rm, review, records)
            for rm, review, records in mod_review_changes
        ]
        enhancement_summary = self.calculate_enhancement_summary(
            modifications_applied
        )

        enhanced_recipe = EnhancedRecipe(
            recipe_id=f"{original_recipe.recipe_id}_enhanced",
            original_recipe_id=original_recipe.recipe_id,
            title=f"{original_recipe.title} (Community Enhanced)",
            original_ingredients=original_recipe.ingredients,
            original_instructions=original_recipe.instructions,
            ingredients=modified_recipe.ingredients,
            instructions=modified_recipe.instructions,
            line_diffs=line_diffs,
            modifications_applied=modifications_applied,
            enhancement_summary=enhancement_summary,
            description=original_recipe.description,
            servings=original_recipe.servings,
            prep_time=getattr(original_recipe, "prep_time", None),
            cook_time=getattr(original_recipe, "cook_time", None),
            total_time=getattr(original_recipe, "total_time", None),
            created_at=datetime.now().isoformat(),
            pipeline_version=self.pipeline_version,
        )

        logger.info(
            f"Generated enhanced recipe with {enhancement_summary.total_changes} changes "
            f"from {len(modifications_applied)} modifications, "
            f"{len(line_diffs)} line diffs"
        )
        return enhanced_recipe

    def generate_comparison_data(
        self, original_recipe: Recipe, enhanced_recipe: EnhancedRecipe
    ) -> Dict[str, Any]:
        comparison = {
            "original": {
                "title": original_recipe.title,
                "ingredients": original_recipe.ingredients,
                "instructions": original_recipe.instructions,
                "servings": original_recipe.servings,
            },
            "enhanced": {
                "title": enhanced_recipe.title,
                "ingredients": enhanced_recipe.ingredients,
                "instructions": enhanced_recipe.instructions,
                "servings": enhanced_recipe.servings,
            },
            "line_diffs": [d.model_dump() for d in enhanced_recipe.line_diffs],
            "changes": {
                "total_modifications": len(enhanced_recipe.modifications_applied),
                "total_changes": enhanced_recipe.enhancement_summary.total_changes,
                "change_types": enhanced_recipe.enhancement_summary.change_types,
                "expected_impact": enhanced_recipe.enhancement_summary.expected_impact,
            },
            "citations": [
                {
                    "reviewer": mod.source_review.reviewer,
                    "rating": mod.source_review.rating,
                    "modification_type": mod.modification_type,
                    "reasoning": mod.reasoning,
                    "mention_count": mod.mention_count,
                    "changes": [
                        {
                            "type": change.type,
                            "from": change.from_text,
                            "to": change.to_text,
                            "operation": change.operation,
                        }
                        for change in mod.changes_made
                    ],
                }
                for mod in enhanced_recipe.modifications_applied
            ],
        }
        return comparison

    def save_enhanced_recipe(
        self, enhanced_recipe: EnhancedRecipe, output_path: str
    ) -> str:
        import json
        import os

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(enhanced_recipe.model_dump(), f, indent=2, ensure_ascii=False)

        logger.info(f"Saved enhanced recipe to: {output_path}")
        return output_path
