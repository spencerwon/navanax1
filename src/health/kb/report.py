"""KB summary: what is in the knowledge base, and how well it is graded.

`summary(kb)` counts records by entity type, scale, relation type, evidence
sourceType and defaultGrade, and grades the KB quantities and the engine
parameters. The headline metric is `paramsGradedAtLeastB`: the share of engine
parameters whose grade is B-textbook or better (A-meta, A-primary, B-textbook).

Counting is tolerant: a malformed record never raises here --
`health.kb.check.check_kb` is the place that reports it. A field that is absent is
counted under "(none)"; one of the wrong JSON type (a list, an object, a number
where a string belongs) under "(invalid)".

HREQ-N-01: when the engine parameter table cannot support the >= B share or the
E-assumption count (no usable params.json; a row without a valid grade), those are
"unavailable" -- `paramsUnavailable` says why -- never a plausible 0 or "n/a".
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from . import records
from .check import GRADES

GRADE_B_OR_BETTER = GRADES[: GRADES.index("B-textbook") + 1]
NONE = "(none)"
INVALID = "(invalid)"
UNAVAILABLE = "unavailable"


def _dicts(items: list[Any]) -> list[dict[str, Any]]:
    return [x for x in items if isinstance(x, dict)]


def _category(rec: dict[str, Any], key: str) -> str:
    """A categorical field as a counting key: its text, "(none)" or "(invalid)"."""
    if key not in rec:
        return NONE
    v = rec[key]
    return v if isinstance(v, str) else INVALID


def _scale(rec: dict[str, Any]) -> int | str:
    if "scale" not in rec:
        return NONE
    v = rec["scale"]
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    if isinstance(v, float) and math.isfinite(v) and v.is_integer():
        return int(v)
    return INVALID


def _by_count(counter: Counter) -> dict[str, int]:
    """Most frequent first, then by key -- stable, readable output."""
    return {str(k): n for k, n in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))}


def _by_grade(recs: list[dict[str, Any]], key: str) -> dict[str, int]:
    """Every grade in rank order (zeros kept: an absent grade is information), then
    anything off-contract under its own name, then "(invalid)" and "(none)"."""
    c = Counter(_category(r, key) for r in recs)
    out = {g: c.pop(g, 0) for g in GRADES}
    tail = {k: c.pop(k) for k in (INVALID, NONE) if k in c}
    out.update(_by_count(c))
    out.update(tail)
    return out


def _scale_key(scale: Any) -> tuple[int, int, str]:
    """Integer scales ascending, then "(invalid)", then "(none)"."""
    if isinstance(scale, int):
        return (0, scale, "")
    return (1 if scale == INVALID else 2, 0, str(scale))


def _share(grades: list[Any]) -> dict[str, Any]:
    n = sum(1 for g in grades if g in GRADE_B_OR_BETTER)
    total = len(grades)
    return {"count": n, "total": total, "share": (n / total) if total else None}


def params_problem(params_doc: Any, load_error: str | None = None,
                   path: str | None = None) -> str | None:
    """Why the parameter table cannot support the >= B share and E count, or None."""
    if load_error:
        return load_error
    if params_doc is None:
        return f"{path} holds null, not an object of rows" if path else "no params.json found"
    if not isinstance(params_doc, dict):
        return f"params.json is a JSON {type(params_doc).__name__}, not an object of rows"
    if not params_doc:
        return "params.json has no rows"
    bad = sorted(str(name) for name, row in params_doc.items()
                 if not isinstance(row, dict) or row.get("grade") not in GRADES)
    if bad:
        shown = ", ".join(bad[:5]) + (" ..." if len(bad) > 5 else "")
        return f"{len(bad)} params.json row(s) have no valid grade ({shown})"
    return None


def summary(kb: dict[str, Any]) -> dict[str, Any]:
    ents = _dicts(records(kb, "entities"))
    rels = _dicts(records(kb, "relations"))
    evs = _dicts(records(kb, "evidence"))
    log = _dicts(records(kb, "verification"))
    quantities = [r["quantity"] for r in rels if isinstance(r.get("quantity"), dict)]
    conflicts = sum(len(x["conflicts"]) for x in rels + quantities
                    if isinstance(x.get("conflicts"), list))
    params_doc = kb.get("params")
    errors = kb.get("load_errors") if isinstance(kb.get("load_errors"), dict) else {}
    paths = kb.get("paths") if isinstance(kb.get("paths"), dict) else {}
    problem = params_problem(params_doc, errors.get("params"), paths.get("params"))
    params = _dicts(list(params_doc.values())) if isinstance(params_doc, dict) else []
    meta_doc = kb.get("entities") if isinstance(kb.get("entities"), dict) else {}

    scales = Counter(_scale(e) for e in ents)
    if problem is None:
        params_share: dict[str, Any] = _share([p.get("grade") for p in params])
    else:
        params_share = {"count": None, "total": None, "share": None, UNAVAILABLE: problem}
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
            "params": len(params_doc) if isinstance(params_doc, dict) else None,
        },
        "entitiesByType": _by_count(Counter(_category(e, "type") for e in ents)),
        "entitiesByScale": {str(k): scales[k] for k in sorted(scales, key=_scale_key)},
        "relationsByType": _by_count(Counter(_category(r, "type") for r in rels)),
        "evidenceBySourceType": _by_count(Counter(_category(v, "sourceType") for v in evs)),
        "evidenceByDefaultGrade": _by_grade(evs, "defaultGrade"),
        "quantitiesByGrade": _by_grade(quantities, "grade"),
        "paramsByGrade": _by_grade(params, "grade") if isinstance(params_doc, dict) else {},
        "quantitiesGradedAtLeastB": _share([q.get("grade") for q in quantities]),
        "paramsGradedAtLeastB": params_share,
        "paramsUnavailable": problem,
    }


def format_share(share: dict[str, Any]) -> str:
    if share.get(UNAVAILABLE):
        return UNAVAILABLE
    if share["share"] is None:
        return "n/a (none)"
    return f"{share['count']}/{share['total']} ({100.0 * share['share']:.1f}%)"


def assumption_count(s: dict[str, Any]) -> str:
    """The engine parameters' E-assumption count as displayed: a number or "unavailable"."""
    if s.get("paramsUnavailable"):
        return UNAVAILABLE
    return str(s["paramsByGrade"].get("E-assumption", 0))


def format_summary(s: dict[str, Any]) -> str:
    """The summary as aligned plain text."""
    lines = [f"KB {s.get('kbVersion')}  generated {s.get('generated')}  "
             f"curator {s.get('curator')}"]

    def section(title: str, table: dict[str, Any], empty: str = "  (none)") -> None:
        lines.append("")
        lines.append(title)
        if not table:
            lines.append(empty)
            return
        width = max(len(str(k)) for k in table)
        for k, v in table.items():
            shown = UNAVAILABLE if v is None else v
            lines.append(f"  {str(k):<{width}}  {shown:>5}")

    section("counts", s["counts"])
    section("entities by type", s["entitiesByType"])
    section("entities by scale", s["entitiesByScale"])
    section("relations by type", s["relationsByType"])
    section("evidence by sourceType", s["evidenceBySourceType"])
    section("evidence by defaultGrade", s["evidenceByDefaultGrade"])
    section("KB quantities by grade", s["quantitiesByGrade"])
    section("engine params by grade", s["paramsByGrade"],
            f"  ({UNAVAILABLE}: {s.get('paramsUnavailable')})")
    lines.append("")
    lines.append(f"KB quantities graded >= B   {format_share(s['quantitiesGradedAtLeastB'])}")
    params_line = format_share(s["paramsGradedAtLeastB"])
    if s.get("paramsUnavailable"):
        params_line += f" ({s['paramsUnavailable']})"
    lines.append(f"engine params graded >= B   {params_line}")
    return "\n".join(lines)


__all__ = ["GRADE_B_OR_BETTER", "INVALID", "NONE", "UNAVAILABLE", "assumption_count",
           "format_share", "format_summary", "params_problem", "summary"]
