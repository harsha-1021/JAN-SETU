"""
Priority scoring — turns raw complaint counts into ranked regional priorities.

    Priority = 100 x (
        0.55 x DemandIntensity
      + 0.30 x Severity
      + 0.15 x CategoryImportance
    ) x InvestmentAdjustment

- DemandIntensity blends a Bayesian-smoothed complaint rate (protects small
  regions from small-sample noise) with log-scaled absolute reach (protects
  a big, genuinely-worse-off city from being outranked by a tiny sample).
- UrgencyFactor comes from average complaint severity (0.5-1.0 range, so it
  modulates rather than zeroing a region out).
- InvestmentPenalty subtracts regions that were already recently funded.

This file has zero database or web dependencies on purpose, so the scoring
logic itself can be unit-tested without spinning up FastAPI or SQLite.
"""

import json
import math

CATEGORY_WEIGHTS = {
    "health": 5,
    "water": 4,
    "sanitation": 3,
    "roads": 2,
    "power": 2,
    "education": 2,
    "other": 1,
}

VALID_CATEGORIES = list(CATEGORY_WEIGHTS.keys())

# Auto-routing: which government body a forwarded complaint goes to,
# based on category. Mocked for the prototype — swap for a real
# jurisdiction/contact directory before any real deployment.
ESCALATION_TARGETS = {
    "health": "State Health Department",
    "water": "State Water Resources Department",
    "sanitation": "Urban Sanitation Directorate",
    "roads": "Public Works Department (PWD)",
    "power": "State Electricity Board",
    "education": "State Education Department",
    "other": "District Collector's Office",
}

# Keyword-based classification — kept as a fast, dependency-free FALLBACK
# for when the LLM call below fails or times out. Not the primary path
# anymore; see classify_category_llm().
CATEGORY_KEYWORDS = {
    "health": [
        "hospital", "clinic", "doctor", "medicine", "disease", "sick",
        "ambulance", "epidemic", "outbreak", "infection", "illness",
    ],
    "water": [
        "water", "pipe", "tap", "borewell", "supply", "leak",
        "contaminated", "drinking water",
    ],
    "sanitation": [
        "sewage", "drain", "toilet", "garbage", "waste", "sanitation", "dump",
    ],
    "roads": [
        "road", "pothole", "street", "highway", "bridge", "traffic", "pavement",
    ],
    "power": [
        "electricity", "power", "outage", "transformer", "voltage", "blackout",
    ],
    "education": [
        "school", "teacher", "education", "college", "student", "classroom",
    ],
}

CATEGORY_TIEBREAK_ORDER = ["health", "water", "sanitation", "roads", "power", "education"]

# Per-complaint urgency signals — duration/critical-facility/emergency language
# pushes a single complaint's severity score up, independent of category.
SEVERITY_KEYWORDS = {
    "high": [
        "days", "weeks", "hospital", "emergency", "contaminated",
        "died", "dead", "no water", "no power", "urgent",
    ],
    "medium": ["often", "sometimes", "frequent", "repeated"],
}

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": VALID_CATEGORIES},
    },
    "required": ["category"],
    "additionalProperties": False,
}

_CLASSIFY_SYSTEM_PROMPT = (
    "You classify a citizen's infrastructure development complaint into "
    "exactly one category: health, water, sanitation, roads, power, "
    "education, or other. This platform only handles infrastructure "
    "planning requests, not live emergencies — classify by the underlying "
    "civic issue, not by urgency. Use 'other' only if truly none fit."
)


def classify_category_llm(text: str, sarvam_client) -> str:
    """Primary classifier — uses Sarvam's chat completion API (sarvam-105b)
    with a strict JSON schema, so it understands paraphrasing and context
    that a fixed keyword list never will. Falls back to classify_category()
    (keyword-based) if the API call fails, so one flaky request never
    breaks the intake pipeline."""
    try:
        response = sarvam_client.chat.completions(
            model="sarvam-105b",
            messages=[
                {"role": "system", "content": _CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            reasoning_effort=None,  # classification doesn't need thinking mode - keeps latency/cost down
            request_options={
                "additional_body_parameters": {
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "complaint_category",
                            "strict": True,
                            "schema": _CLASSIFY_SCHEMA,
                        },
                    }
                }
            },
        )
        result = json.loads(response.choices[0].message.content)
        category = result.get("category", "other")
        return category if category in CATEGORY_WEIGHTS else "other"
    except Exception:
        return classify_category(text)


def classify_category(text: str) -> str:
    """Keyword-based fallback classifier — see classify_category_llm() for
    the primary path."""
    text_lower = text.lower()
    scores = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw in text_lower)
        if count > 0:
            scores[category] = count

    if not scores:
        return "other"

    max_count = max(scores.values())
    tied_categories = [c for c in CATEGORY_TIEBREAK_ORDER if scores.get(c) == max_count]
    return tied_categories[0] if tied_categories else max(scores, key=scores.get)


def score_severity(text: str) -> float:
    text_lower = text.lower()
    if any(kw in text_lower for kw in SEVERITY_KEYWORDS["high"]):
        return 0.9
    if any(kw in text_lower for kw in SEVERITY_KEYWORDS["medium"]):
        return 0.5
    return 0.3

# Mock population + "already funded" data for the prototype demo.
# Swap this for a real census/investment dataset before any real deployment.
REGION_POPULATION = {
    "Chennai": 7_000_000,
    "Bangalore": 8_500_000,
}
DEFAULT_POPULATION = 500_000

REGION_INVESTMENT_PENALTY = {
    "Chennai": 0.1,
    "Bangalore": 0.6,
}
DEFAULT_INVESTMENT_PENALTY = 0.2

PRIOR_POPULATION_STRENGTH = 100


def smoothed_rate(
    unique_reporters: int,
    population: int,
    baseline_rate: float,
    prior_strength: int = PRIOR_POPULATION_STRENGTH,
) -> float:
    """Empirical-Bayes reporting rate.

    Low-evidence regions are pulled toward the observed system-wide reporting
    rate, not an arbitrary 50% prior. ``prior_strength`` is expressed as an
    equivalent population so it works with census denominators.
    """
    if population <= 0:
        raise ValueError("population must be positive")
    baseline = min(max(baseline_rate, 0.0), 1.0)
    return (unique_reporters + prior_strength * baseline) / (population + prior_strength)


def demand_intensity(
    unique_reporters: int,
    population: int,
    baseline_rate: float,
    max_smoothed_rate: float,
    max_log_reporters: float,
) -> float:
    """Blend population-normalized demand with absolute community reach."""
    rate = smoothed_rate(unique_reporters, population, baseline_rate)
    rate_term = rate / max_smoothed_rate if max_smoothed_rate > 0 else 0.0
    log_reporters = math.log1p(unique_reporters)
    scale_term = log_reporters / max_log_reporters if max_log_reporters > 0 else 0.0
    return min(1.0, 0.65 * rate_term + 0.35 * scale_term)


def urgency_factor(avg_severity: float) -> float:
    """Return a bounded severity component for the explainable score."""
    return min(max(avg_severity, 0.0), 1.0)


def priority_score(
    category: str,
    unique_reporters: int,
    population: int,
    avg_severity: float,
    baseline_rate: float,
    max_smoothed_rate: float,
    max_log_reporters: float,
    investment_penalty: float,
) -> float:
    return priority_breakdown(
        category, unique_reporters, population, avg_severity, baseline_rate,
        max_smoothed_rate, max_log_reporters, investment_penalty,
    )["priority_score"]


def priority_breakdown(
    category: str,
    unique_reporters: int,
    population: int,
    avg_severity: float,
    baseline_rate: float,
    max_smoothed_rate: float,
    max_log_reporters: float,
    investment_penalty: float,
) -> dict:
    """Same math as priority_score(), but returns every component so the
    dashboard can show policymakers WHY a region ranked where it did —
    not just the final number."""
    weight = CATEGORY_WEIGHTS.get(category, 1)
    category_importance = weight / max(CATEGORY_WEIGHTS.values())
    intensity = demand_intensity(
        unique_reporters, population, baseline_rate,
        max_smoothed_rate, max_log_reporters,
    )
    urgency = urgency_factor(avg_severity)
    raw_score = 0.55 * intensity + 0.30 * urgency + 0.15 * category_importance
    investment_adjustment = 1.0 - 0.25 * min(max(investment_penalty, 0.0), 1.0)
    score = 100 * raw_score * investment_adjustment

    return {
        "category_weight": weight,
        "demand_intensity": round(intensity, 4),
        "urgency_factor": round(urgency, 4),
        "investment_penalty": round(investment_penalty, 4),
        "investment_deduction": round(1.0 - investment_adjustment, 4),
        "priority_score": round(score, 2),
    }
