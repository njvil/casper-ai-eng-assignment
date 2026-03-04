"""
Pydantic data models for the LLM Analysis Pipeline v2.

These models define the structure for recipe modifications, enhanced recipes,
and all intermediate data formats used throughout the pipeline.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

MODIFICATION_TYPE = Literal[
    "ingredient_substitution",
    "quantity_adjustment",
    "technique_change",
    "addition",
    "removal",
]


class ModificationEdit(BaseModel):
    """Individual atomic edit operation for a recipe modification."""

    target: Literal["ingredients", "instructions"] = Field(
        description="Whether this edit applies to ingredients or instructions"
    )
    operation: Literal["replace", "add_after", "remove"] = Field(
        default="replace",
        description="Type of operation: replace text, add after target, or remove",
    )
    find: str = Field(description="Text to find in the recipe")
    replace: Optional[str] = Field(
        default=None, description="Replacement text (required for replace operations)"
    )
    add: Optional[str] = Field(
        default=None, description="Text to add (required for add_after operations)"
    )


class ModificationObject(BaseModel):
    """Structured modification parsed from a review."""

    modification_type: MODIFICATION_TYPE = Field(
        description="Primary category of modification"
    )

    reasoning: str = Field(description="Why this modification improves the recipe")

    edits: List[ModificationEdit] = Field(description="List of atomic edits to apply")


class ExtractionResult(BaseModel):
    """Wrapper returned by the LLM for per-review extraction (Phase 1).
    Allows one review to yield multiple distinct modifications."""

    modifications: List[ModificationObject]


class BestSource(BaseModel):
    """Metadata about the highest-quality source review for a ranked modification."""

    username: Optional[str] = None
    rating: Optional[int] = None
    helpful_count: Optional[int] = None
    is_featured: bool = False


class RankedModification(BaseModel):
    """A semantically deduplicated, conflict-resolved modification returned by
    the Phase 2 summarisation LLM call."""

    modification_type: List[MODIFICATION_TYPE] = Field(
        description="One or more categories this modification spans"
    )
    reasoning: str = Field(description="Why this modification improves the recipe")
    edits: List[ModificationEdit] = Field(description="Atomic edits to apply")
    mention_count: int = Field(
        description="How many distinct reviews suggested this modification"
    )
    best_source: BestSource = Field(
        description="Metadata from the highest-quality source review"
    )


class SourceReview(BaseModel):
    """Reference to the original review that suggested the modification."""

    text: str = Field(description="Full text of the original review")
    reviewer: Optional[str] = Field(description="Username of the reviewer")
    rating: Optional[int] = Field(description="Star rating given by reviewer")


class ChangeRecord(BaseModel):
    """Record of a specific change made to the recipe."""

    type: Literal["ingredient", "instruction"] = Field(
        description="Type of element that was changed"
    )
    from_text: str = Field(description="Original text before modification")
    to_text: str = Field(description="New text after modification")
    operation: Literal["replace", "add", "remove"] = Field(
        description="Type of operation performed"
    )


class LineDiff(BaseModel):
    """A single line-level diff for the enhanced recipe output.
    This is the primary data a UI uses to render an inline diff view."""

    section: Literal["ingredients", "instructions"]
    line_index: int = Field(description="0-based index in the original list")
    original: str = Field(description="Original line text (empty string for additions)")
    modified: str = Field(description="Modified line text (empty string for removals)")
    operation: Literal["replace", "add", "remove"]
    source_username: Optional[str] = Field(
        default=None, description="Reviewer who suggested this change"
    )
    reasoning: str = Field(default="", description="Why this change was made")


class ModificationApplied(BaseModel):
    """Full record of a modification that was applied to a recipe."""

    source_review: SourceReview = Field(
        description="Review that suggested this modification"
    )
    modification_type: List[str] = Field(description="Categories of modification")
    reasoning: str = Field(description="Why this modification was applied")
    changes_made: List[ChangeRecord] = Field(
        description="Detailed list of changes made"
    )
    mention_count: int = Field(
        default=1,
        description="How many reviews suggested this same modification",
    )


class EnhancementSummary(BaseModel):
    """Summary of all modifications applied to a recipe."""

    total_changes: int = Field(description="Total number of changes made")
    change_types: List[str] = Field(description="Types of modifications applied")
    expected_impact: str = Field(
        description="Expected improvement from these modifications"
    )


class EnhancedRecipe(BaseModel):
    """Recipe with community modifications applied and full attribution."""

    recipe_id: str = Field(description="Enhanced recipe ID")
    original_recipe_id: str = Field(description="ID of the original recipe")
    title: str = Field(description="Enhanced recipe title")

    # Original recipe content (for side-by-side comparison)
    original_ingredients: List[str] = Field(
        description="Original unmodified ingredients list"
    )
    original_instructions: List[str] = Field(
        description="Original unmodified instructions list"
    )

    # Enhanced recipe content
    ingredients: List[str] = Field(description="Modified ingredients list")
    instructions: List[str] = Field(description="Modified instructions list")

    # Line-level diffs for UI rendering
    line_diffs: List[LineDiff] = Field(
        default_factory=list,
        description="Line-level diffs between original and enhanced recipe",
    )

    # Attribution and tracking
    modifications_applied: List[ModificationApplied] = Field(
        description="Full record of all modifications applied"
    )
    enhancement_summary: EnhancementSummary = Field(
        description="Summary of all enhancements"
    )

    # Optional metadata
    description: Optional[str] = Field(
        default=None, description="Enhanced recipe description"
    )
    servings: Optional[str] = Field(default=None, description="Number of servings")
    prep_time: Optional[str] = Field(default=None, description="Preparation time")
    cook_time: Optional[str] = Field(default=None, description="Cooking time")
    total_time: Optional[str] = Field(default=None, description="Total time")

    # Generation metadata
    created_at: str = Field(description="When this enhanced recipe was created")
    pipeline_version: str = Field(
        default="2.0.0", description="Version of the pipeline that created this"
    )


class Recipe(BaseModel):
    """Base recipe model for input data."""

    recipe_id: str
    title: str
    ingredients: List[str]
    instructions: List[str]
    description: Optional[str] = None
    servings: Optional[str] = None
    rating: Optional[Dict[str, Any]] = None


class Review(BaseModel):
    """Review model for input data."""

    text: str
    rating: Optional[int] = None
    username: Optional[str] = None
    has_modification: bool = False
    helpful_count: Optional[int] = None
    feedback_id: Optional[str] = None
    is_featured: bool = False
