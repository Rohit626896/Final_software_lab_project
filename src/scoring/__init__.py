"""Transparent, configurable resource-readiness rubric."""

WEIGHTS = {"code": 35, "dataset": 35, "documentation": 20, "supplementary": 10}
LABELS = {
    "code": "Source code",
    "dataset": "Datasets",
    "documentation": "Documentation",
    "supplementary": "Supplementary material",
}


def calculate_score(resources, applicable=None):
    """Award category weights for accessible links and explain the result."""
    applicable = (
        list(WEIGHTS) if applicable is None else [key for key in WEIGHTS if key in applicable]
    )
    if not applicable:
        raise ValueError("Select at least one applicable resource category.")
    denominator = sum(WEIGHTS[key] for key in applicable)
    breakdown, earned = [], 0
    for category, weight in WEIGHTS.items():
        matches = [r for r in resources if r["category"] == category]
        accessible = sum(r.get("status") == "accessible" for r in matches)
        included = category in applicable
        points = weight if accessible and included else 0
        earned += points
        breakdown.append(
            {
                "category": category,
                "label": LABELS[category],
                "weight": weight,
                "applicable": included,
                "discovered": len(matches),
                "accessible": accessible,
                "earned": points,
            }
        )
    score = round(100 * earned / denominator)
    return {
        "score": score,
        "denominator": denominator,
        "earned": earned,
        "applicable": applicable,
        "breakdown": breakdown,
        "version": "1.0",
        "label": (
            "Strong evidence"
            if score >= 75
            else "Partial evidence" if score >= 40 else "Limited evidence"
        ),
        "explanation": "100 × accessible-category weights / applicable-category weights. "
        "A category earns its weight when at least one matching link responds successfully. "
        "Unresolved and restricted links earn no verified points; they are not proof of absence. "
        "Categories are selected by the reviewer, not inferred requirements. "
        "This score measures linked-resource readiness, not experimental reproducibility.",
    }
