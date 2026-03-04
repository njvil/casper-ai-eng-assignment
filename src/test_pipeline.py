#!/usr/bin/env python3
"""
LLM Analysis Pipeline v2 Test Script

Usage:
    python test_pipeline.py single    # Test single recipe (chocolate chip cookies)
    python test_pipeline.py all       # Process all recipes in data directory
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from llm_pipeline.pipeline import LLMAnalysisPipeline

load_dotenv()


def test_single_recipe():
    """Test the pipeline with the chocolate chip cookie recipe."""

    if not os.getenv("OPENAI_API_KEY"):
        logger.error("OPENAI_API_KEY environment variable not set")
        logger.info("Please set your OpenAI API key in .env file")
        return False

    try:
        pipeline = LLMAnalysisPipeline()
        logger.info("Pipeline initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize pipeline: {e}")
        return False

    recipe_file = "../data/recipe_10813_best-chocolate-chip-cookies.json"
    if not Path(recipe_file).exists():
        logger.error(f"Recipe file not found: {recipe_file}")
        return False

    logger.info(f"Testing with recipe file: {recipe_file}")

    try:
        enhanced_recipe = pipeline.process_single_recipe(
            recipe_file=recipe_file,
            save_output=True,
        )

        if enhanced_recipe:
            logger.success("✓ Single recipe test successful!")
            logger.info(f"Enhanced recipe: {enhanced_recipe.title}")
            logger.info(
                f"Modifications applied: {len(enhanced_recipe.modifications_applied)}"
            )
            logger.info(
                f"Total changes: {enhanced_recipe.enhancement_summary.total_changes}"
            )
            logger.info(f"Line diffs: {len(enhanced_recipe.line_diffs)}")
            logger.info(
                f"Expected impact: {enhanced_recipe.enhancement_summary.expected_impact}"
            )

            for diff in enhanced_recipe.line_diffs:
                op = diff.operation.upper()
                who = diff.source_username or "unknown"
                if diff.operation == "replace":
                    logger.info(
                        f"  [{op}] {diff.section}[{diff.line_index}]: "
                        f'"{diff.original}" → "{diff.modified}" (by {who})'
                    )
                elif diff.operation == "add":
                    logger.info(
                        f"  [{op}] {diff.section}[{diff.line_index}]: "
                        f'+ "{diff.modified}" (by {who})'
                    )
                elif diff.operation == "remove":
                    logger.info(
                        f"  [{op}] {diff.section}[{diff.line_index}]: "
                        f'- "{diff.original}" (by {who})'
                    )

            assert len(enhanced_recipe.line_diffs) > 0, (
                "Expected at least one line diff when modifications were applied"
            )
            assert len(enhanced_recipe.original_ingredients) > 0
            assert len(enhanced_recipe.original_instructions) > 0
            return True
        else:
            logger.error("✗ Single recipe test failed - no enhanced recipe generated")
            return False

    except Exception as e:
        logger.error(f"Single recipe test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_all_recipes():
    """Test the pipeline with all scraped recipes."""

    if not os.getenv("OPENAI_API_KEY"):
        logger.error("OPENAI_API_KEY environment variable not set")
        logger.info("Please set your OpenAI API key in .env file")
        return False

    try:
        pipeline = LLMAnalysisPipeline()
        logger.info("Pipeline initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize pipeline: {e}")
        return False

    try:
        enhanced_recipes = pipeline.process_recipe_directory(data_dir="../data")
        report_path = pipeline.save_summary_report(enhanced_recipes)

        logger.info(f"\n{'=' * 60}")
        logger.success("✓ All recipes test complete!")
        logger.info(f"Enhanced recipes: {len(enhanced_recipes)}")
        logger.info(f"Summary report saved to: {report_path}")

        total_diffs = sum(len(r.line_diffs) for r in enhanced_recipes)
        logger.info(f"Total line diffs across all recipes: {total_diffs}")

        return len(enhanced_recipes) > 0

    except Exception as e:
        logger.error(f"All recipes test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    if len(sys.argv) < 2:
        logger.error("Usage: python test_pipeline.py [single|all]")
        logger.info("  single - Test single chocolate chip cookie recipe")
        logger.info("  all    - Test all recipes in data directory")
        sys.exit(1)

    mode = sys.argv[1].lower()

    if mode == "single":
        logger.info("Starting LLM Analysis Pipeline v2 - Single Recipe Test")
        logger.info("=" * 60)
        success = test_single_recipe()

        logger.info("=" * 60)
        if success:
            logger.success("Single recipe test passed! ✓")
            logger.info(
                "Check the 'data/enhanced/' directory for the enhanced recipe."
            )
        else:
            logger.error("Single recipe test failed! ✗")
            sys.exit(1)

    elif mode == "all":
        logger.info("Starting LLM Analysis Pipeline v2 - All Recipes Validation")
        logger.info("=" * 60)
        success = test_all_recipes()

        logger.info("=" * 60)
        if success:
            logger.success("All recipes validation passed! ✓")
            logger.info(
                "Check 'data/enhanced/' for all enhanced recipes."
            )
            logger.info(
                "Check 'data/enhanced/pipeline_summary_report.json' for results."
            )
        else:
            logger.error("All recipes validation failed! ✗")
            sys.exit(1)

    else:
        logger.error(f"Unknown mode: {mode}")
        logger.error("Usage: python test_pipeline.py [single|all]")
        sys.exit(1)


if __name__ == "__main__":
    main()
