"""Pure review statistics shared by authorized Live queries and Demo fixtures."""


from app.services.welfare_disclosure import threshold


def summarize_review_rows(rows, suppress=False):
    groups = {}
    for row in rows:
        groups.setdefault(row.review_type, []).append(row)
    result = []
    for kind, rows in sorted(groups.items()):
        people = {r.crew_id for r in rows}
        suppressed = bool(suppress and len(people) < threshold())
        distribution = []
        for rating in range(1, 6):
            matches = [r for r in rows if round(r.rating) == rating]
            hidden = bool(suppress and 0 < len({r.crew_id for r in matches}) < threshold())
            distribution.append({"rating": rating, "count": None if suppressed or hidden else len(matches)})
        # Preserve the existing complementary-cell privacy rule exactly.
        if suppress and any(d["count"] is None for d in distribution):
            distribution = [{"rating": rating, "count": None} for rating in range(1, 6)]
        result.append({
            "type": kind, "suppressed": suppressed,
            "reviews": None if suppressed else len(rows),
            "respondents": None if suppressed else len(people),
            "average_rating": None if suppressed else sum(r.rating for r in rows) / len(rows),
            "distribution": distribution,
        })
    return {"groups": result, "coverage": "Driver and facility star ratings. These are not overall shore-leave satisfaction scores. Company detail requires a separate approved disclosure."}
