"""
LLM prompts for recipe modification extraction (v2).

Phase 1: Per-review extraction — few-shot prompt returning a list of modifications.
Phase 2: Summarisation — deduplicates, resolves conflicts, ranks the top 5.
"""

import json
from typing import List

# ---------------------------------------------------------------------------
# Shared system prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert recipe analyst. Your job is to extract structured recipe modifications from user reviews.

When a user shares their experience modifying a recipe, you need to:
1. Identify exactly what changes they made
2. Understand why they made those changes
3. Convert their modifications into structured edit operations

Categories:
- "ingredient_substitution": Replacing one ingredient with another
- "quantity_adjustment": Changing amounts of existing ingredients
- "technique_change": Altering cooking method, temperature, time
- "addition": Adding new ingredients or steps
- "removal": Removing ingredients or steps

Edit operations:
- "replace": Find existing text and replace it
- "add_after": Add new text after finding target text
- "remove": Remove text that matches the find pattern

Be precise with text matching - use the exact text from the original recipe when possible."""

# ---------------------------------------------------------------------------
# Phase 1: Per-review extraction (few-shot)
# ---------------------------------------------------------------------------

FEW_SHOT_EXAMPLES = [
    {
        "review": "I used a half cup of sugar and one-and-a-half cups of brown sugar instead of the recipe amounts. Made the cookies much more chewy and flavorful!",
        "expected_output": {
            "modifications": [
                {
                    "modification_type": "quantity_adjustment",
                    "reasoning": "Makes cookies more chewy and flavorful by increasing brown sugar ratio",
                    "edits": [
                        {
                            "target": "ingredients",
                            "operation": "replace",
                            "find": "1 cup white sugar",
                            "replace": "0.5 cup white sugar",
                        },
                        {
                            "target": "ingredients",
                            "operation": "replace",
                            "find": "1 cup packed brown sugar",
                            "replace": "1.5 cups packed brown sugar",
                        },
                    ],
                }
            ]
        },
    },
    {
        "review": "I added a teaspoon of cream of tartar to the batter and omitted the water. The cookies retained their shape and didn't spread when baked.",
        "expected_output": {
            "modifications": [
                {
                    "modification_type": "addition",
                    "reasoning": "Helps cookies retain shape and prevents spreading during baking",
                    "edits": [
                        {
                            "target": "ingredients",
                            "operation": "add_after",
                            "find": "0.5 teaspoon salt",
                            "add": "1 teaspoon cream of tartar",
                        },
                    ],
                },
                {
                    "modification_type": "removal",
                    "reasoning": "Omitting water simplifies the recipe without affecting rise",
                    "edits": [
                        {
                            "target": "ingredients",
                            "operation": "remove",
                            "find": "2 teaspoons hot water",
                        },
                    ],
                },
            ]
        },
    },
    {
        "review": "I baked them at 375 degrees instead of 350 for about 8-9 minutes. They came out perfectly crispy on the edges.",
        "expected_output": {
            "modifications": [
                {
                    "modification_type": "technique_change",
                    "reasoning": "Higher temperature and shorter time creates crispier edges",
                    "edits": [
                        {
                            "target": "instructions",
                            "operation": "replace",
                            "find": "350 degrees F",
                            "replace": "375 degrees F",
                        },
                        {
                            "target": "instructions",
                            "operation": "replace",
                            "find": "about 10 minutes",
                            "replace": "about 8-9 minutes",
                        },
                    ],
                }
            ]
        },
    },
]


def build_few_shot_prompt(
    review_text: str, title: str, ingredients: list, instructions: list
) -> str:
    """Build a few-shot prompt for Phase 1 per-review extraction."""

    examples_text = "\n\n".join(
        f'Example {i + 1}:\nReview: "{ex["review"]}"\nOutput: {json.dumps(ex["expected_output"], indent=2)}'
        for i, ex in enumerate(FEW_SHOT_EXAMPLES)
    )

    return f"""{SYSTEM_PROMPT}

Here are some examples of how to extract modifications:

{examples_text}

Now extract from this review:

Original Recipe:
Title: {title}
Ingredients: {json.dumps(ingredients)}
Instructions: {json.dumps(instructions)}

User Review: "{review_text}"

Extract ALL recipe modifications from this review. If the review contains multiple
distinct modifications (e.g. changing sugar AND removing nuts), return each as a
separate object in the modifications array.

Output a JSON object:
{{
    "modifications": [
        {{
            "modification_type": "quantity_adjustment|ingredient_substitution|technique_change|addition|removal",
            "reasoning": "Brief explanation of why this modification improves the recipe",
            "edits": [
                {{
                    "target": "ingredients|instructions",
                    "operation": "replace|add_after|remove",
                    "find": "exact text to find in the recipe",
                    "replace": "replacement text (for replace operations)",
                    "add": "text to add (for add_after operations)"
                }}
            ]
        }}
    ]
}}

Focus on concrete changes the user actually made, not general suggestions."""


# ---------------------------------------------------------------------------
# Phase 2: Summarise, deduplicate, resolve conflicts
# ---------------------------------------------------------------------------

def build_summarize_prompt(
    recipe_title: str,
    ingredients: List[str],
    instructions: List[str],
    raw_modifications_json: str,
) -> str:
    """Build the Phase 2 summarisation prompt.

    Takes the full pool of extracted modifications (with source metadata) and
    asks the LLM to merge duplicates, resolve conflicts, and return a ranked
    top 5.
    """

    return f"""{SYSTEM_PROMPT}

You are now performing a SUMMARISATION step. You have been given a pool of raw
modifications extracted from multiple community reviews of the recipe
"{recipe_title}".

Original recipe for reference:
Ingredients: {json.dumps(ingredients)}
Instructions: {json.dumps(instructions)}

Your job:
1. MERGE semantically identical modifications — e.g. "reduce white sugar to
   half a cup" and "I only used 1/2 cup white sugar" are the same modification.
   Combine their mention counts and keep the best source metadata.
2. DETECT CONFLICTS — when two modifications target the same ingredient or
   instruction but change it in opposite directions (e.g. "more sugar" vs
   "less sugar"), keep only the winning one:
   - Prefer the one from a featured tweak (is_featured=true)
   - If both/neither are featured, prefer higher rating
   - If ratings tie, prefer higher helpful_count
   - Discard the losing modification entirely
3. RANK the surviving unique modifications by:
   mention_count DESC, then best rating DESC, then best helpful_count DESC
4. Return the top 5 (or fewer if less than 5 unique modifications exist).

IMPORTANT: The "find" values in edits must use the EXACT text from the original
recipe ingredients/instructions listed above.

Input pool of raw modifications:
{raw_modifications_json}

Output a JSON object:
{{
    "ranked_modifications": [
        {{
            "modification_type": ["category1", ...],
            "reasoning": "merged reasoning for this modification",
            "edits": [
                {{
                    "target": "ingredients|instructions",
                    "operation": "replace|add_after|remove",
                    "find": "exact text from original recipe",
                    "replace": "replacement text (for replace ops)",
                    "add": "text to add (for add_after ops)"
                }}
            ],
            "mention_count": 3,
            "best_source": {{
                "username": "reviewer_name",
                "rating": 5,
                "helpful_count": 100,
                "is_featured": true
            }}
        }}
    ]
}}"""


# Keep the old simple prompt for backward compatibility / testing
def build_simple_prompt(
    review_text: str, title: str, ingredients: list, instructions: list
) -> str:
    """Build a simple prompt without examples for faster processing."""
    return build_few_shot_prompt(review_text, title, ingredients, instructions)
