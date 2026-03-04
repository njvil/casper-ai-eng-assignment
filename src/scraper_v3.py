"""
Playwright-based AllRecipes scraper (standalone).

Uses a headless Chromium browser so that JS-rendered sections — including
the Featured Tweaks carousel — are fully present in the HTML before parsing.

Setup (one-time):
    uv run playwright install chromium

Usage:
    uv run python src/scraper_playwright.py
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Extra ms to wait after networkidle for Vue/JS carousels to finish rendering
JS_SETTLE_MS = 3_000


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_rendered_html(url: str, js_settle_ms: int = JS_SETTLE_MS) -> str:
    """Return fully rendered HTML for *url* via a headless Chromium browser."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        # Block images/fonts/media to speed up load
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in {"image", "font", "media"}
            else route.continue_(),
        )

        # "load" waits for the window load event; "networkidle" times out on
        # ad-heavy sites like AllRecipes that fire continuous background requests.
        page.goto(url, wait_until="load", timeout=60_000)
        # Extra settle time for Vue/JS carousels to finish rendering
        page.wait_for_timeout(js_settle_ms)
        html = page.content()
        browser.close()
        return html


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def extract_review_data(review_elem) -> Dict:
    """Extract review/tweak data from a review element."""
    review_data = {}

    text_selectors = [
        ("div", {"class": "ugc-review__text"}),
        ("div", {"class": re.compile(r"ugc-review__text")}),
        ("div", {"class": re.compile(r"recipe-review__text")}),
        ("div", {"class": re.compile(r"ReviewText")}),
        ("div", {"class": re.compile(r"ugc-review-body")}),
        ("p", {"class": re.compile(r"review")}),
    ]

    for tag, attrs in text_selectors:
        text_elem = review_elem.find(tag, attrs)
        if text_elem:
            review_text = text_elem.get_text(strip=True)
            if review_text:
                review_data["text"] = review_text
                break

    rating_selectors = [
        ("div", {"class": "ugc-review__rating"}),
        ("div", {"class": re.compile(r"ugc-review__rating")}),
        ("span", {"class": re.compile(r"rating-stars")}),
        ("div", {"class": re.compile(r"RatingStar")}),
        ("span", {"aria-label": re.compile(r"rated \d+ out of 5")}),
    ]

    for tag, attrs in rating_selectors:
        rating_elem = review_elem.find(tag, attrs)
        if rating_elem:
            aria_label = rating_elem.get("aria-label", "")
            rating_match = re.search(r"rated (\d+)", aria_label)
            if rating_match:
                review_data["rating"] = int(rating_match.group(1))
            else:
                stars = rating_elem.find_all("svg", {"class": "icon-star"})
                if stars:
                    review_data["rating"] = len(stars)
            break

    user_selectors = [
        ("span", {"class": re.compile(r"recipe-review__author")}),
        ("span", {"class": re.compile(r"reviewer-name")}),
        ("a", {"class": re.compile(r"cook-name")}),
    ]

    for tag, attrs in user_selectors:
        user_elem = review_elem.find(tag, attrs)
        if user_elem:
            review_data["username"] = user_elem.get_text(strip=True)
            break

    date_elem = review_elem.find(
        ["span", "time"], {"class": re.compile(r"recipe-review__date")}
    )
    if date_elem:
        review_data["date"] = date_elem.get_text(strip=True)

    if review_data.get("text"):
        tweak_patterns = [
            r"I (added|used|substituted|replaced|made with|changed)",
            r"(instead of|rather than|in place of)",
            r"(next time|will make again|definitely make)",
            r"(doubled|tripled|halved|increased|decreased)",
            r"(more|less|extra) ([\w\s]+)",
        ]
        for pattern in tweak_patterns:
            if re.search(pattern, review_data["text"], re.IGNORECASE):
                review_data["has_modification"] = True
                break

    return review_data


def extract_featured_tweak_card(card_elem) -> Dict:
    """Extract data from a Featured Tweaks carousel card."""
    tweak_data: Dict[str, Any] = {}

    feedback_id = card_elem.get("data-feedback-id")
    if feedback_id:
        tweak_data["feedback_id"] = feedback_id

    username_elem = card_elem.select_one(
        ".mm-recipes-ugc-shared-card-byline__username-text"
    )
    if username_elem:
        tweak_data["username"] = username_elem.get_text(strip=True)

    rating_container = card_elem.select_one(".mm-recipes-ugc-shared-star-rating")
    if rating_container:
        filled_stars = 0
        for star_span in rating_container.select(".mm-recipes-ugc-shared-star-rating__star"):
            outline_icon = star_span.find(
                "svg", {"class": re.compile(r"ugc-shared-icon-star-outline")}
            )
            if not outline_icon:
                filled_stars += 1
        if filled_stars:
            tweak_data["rating"] = filled_stars

    date_elem = card_elem.select_one(".mm-recipes-ugc-shared-card-meta__date")
    if date_elem:
        tweak_data["date"] = date_elem.get_text(strip=True)

    text_elem = card_elem.select_one(".mm-recipes-ugc-shared-item-card__text")
    if text_elem:
        text = text_elem.get_text(strip=True)
        if text:
            tweak_data["text"] = text

    helpful_button = card_elem.select_one(".mm-recipes-ugc-shared-helpful-button")
    if helpful_button:
        btn_text = helpful_button.get_text(strip=True)
        parts = btn_text.split()
        if parts and parts[-1].isdigit():
            tweak_data["helpful_count"] = int(parts[-1])

    chips = [
        chip.get_text(strip=True)
        for chip in card_elem.select(".mm-recipes-ugc-shared-review-chips__text")
        if chip.get_text(strip=True)
    ]
    if chips:
        tweak_data["chips"] = chips

    if tweak_data:
        tweak_data["is_featured"] = True
        tweak_data["has_modification"] = True

    return tweak_data


def extract_recipe_from_json_ld(data: Any) -> Optional[Dict]:
    """Extract recipe data from various JSON-LD formats."""
    if isinstance(data, dict):
        types = data.get("@type", [])
        if isinstance(types, list) and "Recipe" in types:
            return data
        elif types == "Recipe":
            return data
    elif isinstance(data, list):
        for item in data:
            recipe = extract_recipe_from_json_ld(item)
            if recipe:
                return recipe
    return None


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

def scrape_allrecipes(url: str) -> Optional[Dict]:
    """
    Scrape recipe data from an AllRecipes URL using a headless browser.

    Returns a dict with recipe data or None if scraping fails.
    """
    try:
        print(f"  [playwright] Loading: {url}")
        html = fetch_rendered_html(url)
        soup = BeautifulSoup(html, "html.parser")

        recipe_data: Dict[str, Any] = {
            "url": url,
            "scraped_at": datetime.now().isoformat(),
        }

        # Recipe ID from URL
        url_parts = url.split("/")
        for i, part in enumerate(url_parts):
            if part == "recipe" and i + 1 < len(url_parts):
                recipe_data["recipe_id"] = url_parts[i + 1]
                break

        # Title from H1 (overridden by JSON-LD below if available)
        title_element = soup.find("h1")
        if title_element:
            recipe_data["title"] = title_element.text.strip()

        # JSON-LD structured data
        recipe_found = None
        for json_ld in soup.find_all("script", type="application/ld+json"):
            try:
                structured_data = json.loads(json_ld.string)
                recipe_found = extract_recipe_from_json_ld(structured_data)
                if recipe_found:
                    break
            except (json.JSONDecodeError, TypeError) as e:
                print(f"Failed to parse JSON-LD: {e}")
                continue

        if recipe_found:
            recipe_data["title"] = recipe_found.get("name", recipe_data.get("title", ""))
            recipe_data["description"] = recipe_found.get("description", "")

            if "aggregateRating" in recipe_found:
                recipe_data["rating"] = {
                    "value": recipe_found["aggregateRating"].get("ratingValue"),
                    "count": recipe_found["aggregateRating"].get("ratingCount"),
                }

            for time_field in ["prepTime", "cookTime", "totalTime"]:
                if time_field in recipe_found:
                    recipe_data[time_field.lower()] = recipe_found[time_field]

            recipe_yield = recipe_found.get("recipeYield")
            if recipe_yield:
                if isinstance(recipe_yield, list):
                    recipe_data["servings"] = recipe_yield[0]
                else:
                    recipe_data["servings"] = str(recipe_yield)

            ingredients = recipe_found.get("recipeIngredient", [])
            if ingredients:
                recipe_data["ingredients"] = ingredients

            instructions = recipe_found.get("recipeInstructions", [])
            if instructions:
                recipe_data["instructions"] = []
                for inst in instructions:
                    if isinstance(inst, dict):
                        text = inst.get("text", inst.get("name", ""))
                        if text:
                            recipe_data["instructions"].append(text)
                    elif isinstance(inst, str):
                        recipe_data["instructions"].append(inst)

            if "nutrition" in recipe_found:
                recipe_data["nutrition"] = recipe_found["nutrition"]

            author = recipe_found.get("author")
            if author:
                if isinstance(author, dict):
                    recipe_data["author"] = author.get("name", str(author))
                else:
                    recipe_data["author"] = str(author)

            recipe_data["categories"] = recipe_found.get("recipeCategory", [])
            if "keywords" in recipe_found:
                keywords = recipe_found["keywords"]
                if isinstance(keywords, str):
                    recipe_data["keywords"] = [k.strip() for k in keywords.split(",")]
                else:
                    recipe_data["keywords"] = keywords

        # Featured Tweaks carousel (JS-rendered — the reason this scraper exists)
        recipe_data["featured_tweaks"] = []
        carousel = soup.find("div", {"class": re.compile(r"mm-recipes-ugc-threaded-carousel")})
        if carousel:
            cards = carousel.find_all(
                "div", {"class": re.compile(r"mm-recipes-ugc-threaded-carousel__card")}
            )
            for card in cards:
                tweak_data = extract_featured_tweak_card(card)
                if tweak_data.get("text"):
                    recipe_data["featured_tweaks"].append(tweak_data)

            print(f"  Extracted {len(recipe_data['featured_tweaks'])} featured tweaks")
        else:
            print("  No Featured Tweaks carousel found in rendered HTML")

        # Reviews
        recipe_data["reviews"] = []
        review_selectors = [
            ("div", {"class": "ugc-review"}),
            ("div", {"class": re.compile(r"ugc-review")}),
            ("div", {"class": re.compile(r"ReviewCard__container")}),
            ("div", {"class": re.compile(r"review-container")}),
            ("article", {"class": re.compile(r"review")}),
        ]

        reviews_found = []
        for tag, attrs in review_selectors:
            reviews_found = soup.find_all(tag, attrs, limit=50)
            if reviews_found:
                print(f"  Found {len(reviews_found)} reviews using selector: {tag} {attrs}")
                break

        for review_elem in reviews_found[:30]:
            review_data = extract_review_data(review_elem)
            if review_data and review_data.get("text"):
                recipe_data["reviews"].append(review_data)

        print(f"  Extracted {len(recipe_data['reviews'])} reviews")
        return recipe_data

    except Exception as e:
        import traceback
        print(f"Error scraping {url}: {e}")
        traceback.print_exc()
        return None


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_recipe_data(recipe_data: Dict, filename: str = None) -> str:
    """Save recipe data to DATA_DIR as JSON."""
    if filename is None:
        recipe_id = recipe_data.get("recipe_id", "unknown")
        title_slug = re.sub(r"[^a-z0-9]+", "-", recipe_data.get("title", "").lower())[:50]
        filename = f"recipe_{recipe_id}_{title_slug}.json"

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    filepath = (
        Path(filename)
        if (os.sep in str(filename) or "/" in str(filename))
        else DATA_DIR / filename
    )

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(recipe_data, f, indent=2, ensure_ascii=False)

    print(f"  Saved → {filepath.resolve()}")
    return str(filepath)


# ---------------------------------------------------------------------------
# Sitemap
# ---------------------------------------------------------------------------

def scrape_sitemap_recipes(limit: int = 50) -> List[str]:
    """Fetch recipe URLs from the AllRecipes sitemap."""
    sitemap_url = "https://www.allrecipes.com/sitemap_1.xml"
    try:
        headers = {"User-Agent": USER_AGENT}
        response = requests.get(sitemap_url, headers=headers)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "xml")
        urls = []
        for loc in soup.find_all("loc"):
            url = loc.text
            if "/recipe/" in url and url not in urls:
                urls.append(url)
                if len(urls) >= limit:
                    break
        return urls

    except Exception as e:
        print(f"Error fetching sitemap: {e}")
        return [
            "https://www.allrecipes.com/recipe/10813/best-chocolate-chip-cookies/",
            "https://www.allrecipes.com/recipe/11679/homemade-mac-and-cheese/",
            "https://www.allrecipes.com/recipe/23600/worlds-best-lasagna/",
            "https://www.allrecipes.com/recipe/24059/creamy-rice-pudding/",
            "https://www.allrecipes.com/recipe/20144/banana-banana-bread/",
        ][:limit]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    test_url = "https://www.allrecipes.com/recipe/10813/best-chocolate-chip-cookies/"

    print("=" * 60)
    print(f"Testing with: {test_url}")
    print("=" * 60)

    recipe_data = scrape_allrecipes(test_url)

    if recipe_data:
        print(f"\n✓ Successfully scraped: {recipe_data.get('title', 'Unknown')}")
        print(f"  Rating: {recipe_data.get('rating', {}).get('value')} "
              f"({recipe_data.get('rating', {}).get('count')} reviews)")
        print(f"  Featured tweaks : {len(recipe_data.get('featured_tweaks', []))}")
        print(f"  Reviews         : {len(recipe_data.get('reviews', []))}")
        reviews_with_mods = [r for r in recipe_data.get("reviews", []) if r.get("has_modification")]
        print(f"  Reviews w/ mods : {len(reviews_with_mods)}")
        print(f"  Has ingredients : {'ingredients' in recipe_data}")
        print(f"  Has instructions: {'instructions' in recipe_data}")
        save_recipe_data(recipe_data)
    else:
        print("✗ Failed to scrape recipe")

    print("\n" + "=" * 60)
    print("Fetching more recipe URLs from sitemap...")
    recipe_urls = scrape_sitemap_recipes(limit=5)
    print(f"Found {len(recipe_urls)} recipe URLs to scrape")

    successful = 0
    for i, url in enumerate(recipe_urls, 1):
        print(f"\n[{i}/{len(recipe_urls)}] Scraping: {url}")
        data = scrape_allrecipes(url)
        if data:
            save_recipe_data(data)
            successful += 1
            print("  ✓ Success")
        else:
            print("  ✗ Failed")

    print("\n" + "=" * 60)
    print(f"Summary: Successfully scraped {successful}/{len(recipe_urls)} recipes")


if __name__ == "__main__":
    main()
