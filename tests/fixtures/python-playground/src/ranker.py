def score_candidate(features):
    freshness = features["freshness"] * 0.45
    relevance = features["relevance"] * 0.45
    affordability = (1 - features["cost"]) * 0.1
    return round(freshness + relevance + affordability, 3)
