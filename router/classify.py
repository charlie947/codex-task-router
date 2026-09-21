"""Deterministic task classification.

Classification is keyword-based and order-sensitive (precedence matters).
No model call is ever made to classify a task; this module only inspects the
task description string supplied by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

LANE_MANUAL_GRAPHIC = "manual_graphic"
LANE_ASTRA = "astra"
LANE_CLAUDE = "claude"
LANE_TERRA = "terra"
LANE_LUNA = "luna"

VALID_LANES = (LANE_MANUAL_GRAPHIC, LANE_ASTRA, LANE_CLAUDE, LANE_TERRA, LANE_LUNA)
OVERRIDABLE_LANES = (LANE_ASTRA, LANE_CLAUDE, LANE_TERRA, LANE_LUNA)

# Ordered precedence groups. First match wins. Each entry is
# (lane, tuple-of-keywords). Keyword match is case-insensitive substring.
GRAPHIC_KEYWORDS = (
    "infographic",
    "graphic",
    "figma",
    "thumbnail",
    "cover image",
    "carousel design",
    "illustration",
    "wireframe",
)

HIGH_RISK_KEYWORDS = (
    "irreversible",
    "production database",
    "prod database",
    "delete all",
    "drop table",
    "force push",
    "force-push",
    "financial trade",
    "wire transfer",
    "legal contract",
    "security vulnerability disclosure",
    "unbounded reasoning",
    "novel safety",
)

BULK_KEYWORDS = (
    "bulk",
    "research",
    "draft",
    "drafting",
    "write a newsletter",
    "write the newsletter",
    "implement",
    "implementation",
    "build the",
    "full build",
    "end-to-end",
    "end to end",
    "large refactor",
)

DIFFICULT_BUILD_KEYWORDS = (
    "difficult",
    "bounded build",
    "fix the bug",
    "debug",
    "refactor",
    "add a feature",
    "write tests",
    "add tests",
    "review the diff",
    "code review",
    "review this pr",
)

ROUTINE_KEYWORDS = (
    "quick question",
    "small edit",
    "typo",
    "rename",
    "one-line",
    "one line",
    "what does",
    "explain",
    "small fix",
)


@dataclass(frozen=True)
class Classification:
    lane: str
    reason: str
    matched_keyword: Optional[str]
    overridden: bool = False


def _first_match(text: str, keywords: tuple) -> Optional[str]:
    lowered = text.lower()
    for kw in keywords:
        if kw in lowered:
            return kw
    return None


def classify(task_text: str, override: Optional[str] = None) -> Classification:
    """Classify a task description into a lane.

    Precedence (automatic classification only, no override supplied):
      1. graphic keywords            -> manual_graphic (never dispatched)
      2. high-risk keywords          -> astra (requires explicit approval)
      3. bulk research/draft/build   -> claude
      4. difficult bounded build     -> terra
      5. default (routine)           -> luna

    An explicit override selects the lane directly and is recorded as such,
    but graphic requests can never be overridden into a worker dispatch: the
    graphic check runs before override handling and always wins.
    """
    if not isinstance(task_text, str) or not task_text.strip():
        raise ValueError("task_text must be a non-empty string")

    graphic_kw = _first_match(task_text, GRAPHIC_KEYWORDS)
    if graphic_kw:
        return Classification(
            lane=LANE_MANUAL_GRAPHIC,
            reason=f"graphic keyword matched: {graphic_kw!r}; graphics stay manual/Figma, never a worker",
            matched_keyword=graphic_kw,
            overridden=False,
        )

    if override is not None:
        if override not in OVERRIDABLE_LANES:
            raise ValueError(
                f"invalid override lane {override!r}; must be one of {OVERRIDABLE_LANES}"
            )
        return Classification(
            lane=override,
            reason=f"explicit override to lane {override!r}",
            matched_keyword=None,
            overridden=True,
        )

    high_risk_kw = _first_match(task_text, HIGH_RISK_KEYWORDS)
    if high_risk_kw:
        return Classification(
            lane=LANE_ASTRA,
            reason=f"high-risk keyword matched: {high_risk_kw!r}; requires named Astra approval",
            matched_keyword=high_risk_kw,
        )

    bulk_kw = _first_match(task_text, BULK_KEYWORDS)
    if bulk_kw:
        return Classification(
            lane=LANE_CLAUDE,
            reason=f"bulk keyword matched: {bulk_kw!r}",
            matched_keyword=bulk_kw,
        )

    difficult_kw = _first_match(task_text, DIFFICULT_BUILD_KEYWORDS)
    if difficult_kw:
        return Classification(
            lane=LANE_TERRA,
            reason=f"difficult-build keyword matched: {difficult_kw!r}",
            matched_keyword=difficult_kw,
        )

    routine_kw = _first_match(task_text, ROUTINE_KEYWORDS)
    return Classification(
        lane=LANE_LUNA,
        reason=(
            f"routine keyword matched: {routine_kw!r}"
            if routine_kw
            else "no keyword matched; default lane is routine/luna"
        ),
        matched_keyword=routine_kw,
    )
