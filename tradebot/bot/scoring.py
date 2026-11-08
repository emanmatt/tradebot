"""Combine follow-signals with the trend score into one ranked buy list."""


def follow_scores(sources: dict, weights: dict) -> dict:
    """sources: {source_name: {ticker: {"score", "reason"}}}

    A perfect signal from your highest-weighted source = 1.0. Lower-weight sources
    count proportionally less, and agreement between sources stacks (capped at 1).
    """
    top_w = max(weights.values())
    out = {}
    for name, sig in sources.items():
        w = weights.get(name, 0)
        for tkr, s in sig.items():
            d = out.setdefault(tkr, {"follow": 0.0, "sources": [], "reasons": []})
            d["follow"] += w * s["score"] / top_w
            d["sources"].append(name)
            d["reasons"].append(f"{name}: {s['reason']}")
    for d in out.values():
        d["follow"] = round(min(1.0, d["follow"]), 3)
    return out


def rank(follow: dict, trend: dict, cfg: dict) -> list[dict]:
    """trend: {ticker: (passes, score, reason, indicators)}. Returns buy candidates, best first."""
    e = cfg["entry"]
    fw = e["follow_weight"]
    picks = []
    for tkr, f in follow.items():
        if len(f["sources"]) < e["min_sources"] or tkr not in trend:
            continue
        ok, tscore, treason, ind = trend[tkr]
        if not ok:
            continue
        final = round(fw * f["follow"] + (1 - fw) * tscore, 3)
        if final >= e["min_score"]:
            picks.append({"ticker": tkr, "score": final, "follow": f["follow"], "trend": tscore,
                          "reasons": f["reasons"] + [f"trend: {treason}"], "ind": ind})
    return sorted(picks, key=lambda p: p["score"], reverse=True)
