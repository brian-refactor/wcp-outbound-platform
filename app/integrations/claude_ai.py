"""
Claude API client for generating personalized email introductions.
"""

import logging

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"


def generate_personalized_intro(prospect) -> str:
    """
    Generate a 1–2 sentence personalized email opener for a prospect.

    Uses the prospect's profile (investor type, geography, asset class, title,
    company) to write a natural opening that flows into the standard sequence body.

    Returns the generated text, or raises an exception if the API call fails.
    """
    if not settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY is not configured")

    profile_lines = []
    if prospect.first_name:
        profile_lines.append(f"First name: {prospect.first_name}")
    if prospect.title:
        profile_lines.append(f"Title: {prospect.title}")
    if prospect.company:
        profile_lines.append(f"Company: {prospect.company}")
    if prospect.geography:
        profile_lines.append(f"Geography: {prospect.geography}")
    if prospect.investor_type:
        label = prospect.investor_type.replace("_", " ").title()
        profile_lines.append(f"Investor type: {label}")
    if prospect.wealth_tier:
        tier_labels = {
            "mass_affluent": "Mass Affluent ($100K–$1M)",
            "HNWI": "High Net Worth ($1M–$30M)",
            "UHNWI": "Ultra High Net Worth ($30M+)",
            "institutional": "Institutional",
        }
        profile_lines.append(f"Wealth tier: {tier_labels.get(prospect.wealth_tier, prospect.wealth_tier)}")
    if prospect.asset_class_preference:
        pref_labels = {"PE": "Private Equity", "RE": "Real Estate", "both": "PE and Real Estate"}
        profile_lines.append(f"Asset class interest: {pref_labels.get(prospect.asset_class_preference, prospect.asset_class_preference)}")

    profile_text = "\n".join(profile_lines) if profile_lines else "No additional profile data available."

    prompt = f"""You are helping write outbound cold emails for Willow Creek Partners, a private markets investment firm that connects accredited investors with institutional-quality private equity and real estate opportunities.

Write a 1–2 sentence personalized opening for a cold email to this prospect. The opener should:
- ONLY reference facts explicitly listed in the prospect profile below — do NOT infer, assume, or fabricate anything not stated
- NOT mention Willow Creek Partners or any fund names (the rest of the email does that)
- NOT start with "I hope this email finds you well" or similar generic openers
- NOT claim to know anything about their current portfolio, activity, or behavior
- Be a natural, professional intro that acknowledges who they are based only on known facts
- Be professional but conversational in tone

Prospect profile:
{profile_text}

Return ONLY the 1–2 sentence opener. Nothing else."""

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    message = client.messages.create(
        model=MODEL,
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )

    intro = message.content[0].text.strip()
    logger.info("Generated personalized intro for %s (%d chars)", prospect.email, len(intro))
    return intro
