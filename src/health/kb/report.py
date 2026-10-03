"""KB summary: what is in the knowledge base, and how well it is graded.

`summary(kb)` counts records by entity type, scale, relation type, evidence
sourceType and defaultGrade, and grades the KB quantities and the engine
parameters. The headline metric is `paramsGradedAtLeastB`: the share of engine
parameters whose grade is B-textbook or better (A-meta, A-primary, B-textbook).

Counting is tolerant: malformed records are skipped rather than raised on --
`health.kb.check.check_kb` is the place that reports them.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from . import records
from .check import GRADES

GRADE_B_OR_BETTER = GRADES[: GRADES.index("B-textbook") + 1]


def _dicts(items: list[Any]) -> list[dict[str, Any]]:
    return [x for x in items if isinstance(x, dict)]


def _by_count(counter: Counter) -> dict[str, int]:
    """Most frequent first, then by key -- stable, readable output."""
    return {str(k): n for k, n in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))}


def _by_grade(grades: list[Any]) -> dict[str, int]:
    """Every grade in rank order (zeros kept: an absent grade is information), then
    anything off-contract under its own name."""
    c = Counter(g for g in grades if isinstance(g, str))
    out = {g: c.pop(g, 0) for g in GRADES}
    out.update(_by_count(c))
    if any(not isinstance(g, str) for g in grades):
        out["(none)"] = sum(1 for g in grades if not isinstance(g, str))
    return out


def _scale_key(scale: Any) -> tuple[int, Any]:
    """Integer scales ascending, then anything malformed by its text."""
    if isinstance(scale, int) and not isinstance(scale, bool):
        return (0, scale)
    return (1, str(scale))


def _share(grades: list[Any]) -> dict[str, Any]:
    n = sum(1 for g in grades if g in GRADE_B_OR_BETTER)
    total = len(grades)
    return {"count": n, "total": total, "share": (n / total) if total else None}


def summary(kb: dict[str, Any]) -> dict[str, Any]:
    ents = _dicts(records(kb, "entities"))
    rels = _dicts(records(kb, "relations"))
    evs = _dicts(records(kb, "evidence"))
    log = _dicts(records(kb, "verification"))
    quantities = [r["quantity"] for r in rels if isinstance(r.get("quantity"), dict)]
    conflicts = sum(len(x["conflicts"]) for x in rels + quantities
                    if isinstance(x.get("conflicts"), list))
    params_doc = kb.get("params")
    params = _dicts(list(params_doc.values())) if isinstance(params_doc, dict) else []
    meta_doc = kb.get("entities") if isinstance(kb.get("entities"), dict) else {}

    scales = Counter(e.get("scale") for e in ents)
    return {
        "kbVersion": meta_doc.get("kbVersion"),
        "generated": meta_doc.get("generated"),
        "curator": meta_doc.get("curator"),
        "counts": {
            "entities": len(ents),
            "relations": len(rels),
            "evidence": len(evs),
            "verificationRecords": len(log),
            "quantities": len(quantities),
            "engineMirrors": sum(1 for q in quantities if "engineParam" in q),
            "conflicts": conflicts,
            "params": len(params),
        },
        "entitiesByType": _by_count(Counter(e.get("type") for e in ents)),
        "entitiesByScale": {str(k): scales[k] for k in sorted(scales, key=_scale_key)},
        "relationsByType": _by_count(Counter(r.get("type") for r in rels)),
        "evidenceBySourceType": _by_count(Counter(v.get("sourceType") for v in evs)),
        "evidenceByDefaultGrade": _by_grade([v.get("defaultGrade") for v in evs]),
        "quantitiesByGrade": _by_grade([q.get("grade") for q in quantities]),
        "paramsByGrade": _by_grade([p.get("grade") for p in params]),
        "quantitiesGradedAtLeastB": _share([q.get("grade") for q in quantities]),
        "paramsGradedAtLeastB": _share([p.get("grade") for p in params]),
    }


def format_share(share: dict[str, Any]) -> str:
    if share["share"] is None:
        return "n/a (none)"
    return f"{share['count']}/{share['total']} ({100.0 * share['share']:.1f}%)"


def format_summary(s: dict[str, Any]) -> str:
    """The summary as aligned plain text."""
    lines = [f"KB {s.get('kbVersion')}  generated {s.get('generated')}  "
             f"curator {s.get('curator')}"]

    def section(title: str, table: dict[str, Any]) -> None:
        lines.append("")
        lines.append(title)
        if not table:
            lines.append("  (none)")
            return
        width = max(len(str(k)) for k in table)
        for k, v in table.items():
            lines.append(f"  {str(k):<{width}}  {v:>5}")

    section("counts", s["counts"])
    section("entities by type", s["entitiesByType"])
    section("entities by scale", s["entitiesByScale"])
    section("relations by type", s["relationsByType"])
    section("evidence by sourceType", s["evidenceBySourceType"])
    section("evidence by defaultGrade", s["evidenceByDefaultGrade"])
    section("KB quantities by grade", s["quantitiesByGrade"])
    section("engine params by grade", s["paramsByGrade"])
    lines.append("")
    lines.append(f"KB quantities graded >= B   {format_share(s['quantitiesGradedAtLeastB'])}")
    lines.append(f"engine params graded >= B   {format_share(s['paramsGradedAtLeastB'])}")
    return "\n".join(lines)


__all__ = ["GRADE_B_OR_BETTER", "format_share", "format_summary", "summary"]
