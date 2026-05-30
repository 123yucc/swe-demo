"""Custom-rule library: load, match, render.

Lives parallel to ``src/memory/retrieve.py`` (the ChromaDB path). Both
paths feed the same ``SharedWorkingMemory`` but write into separate
fields so neither pollutes the other.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.models.custom_rules import CustomRule, RouteTags


_DEFAULT_CUSTOM_KNOWLEDGE_PATH = Path("workdir/long_term_memory/custom_knowledge.json")


def load_custom_rules(
    path: Path | str = _DEFAULT_CUSTOM_KNOWLEDGE_PATH,
) -> list[CustomRule]:
    """Load all custom rules from ``custom_knowledge.json``.

    Missing file => empty list (custom library is optional).
    Malformed entries are skipped with a warning print, not raised, so
    one broken entry does not nuke the whole pipeline.
    """
    p = Path(path)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[custom-route] {p} is not valid JSON: {exc}", flush=True)
        return []

    if not isinstance(raw, dict):
        print(f"[custom-route] {p} top-level must be an object", flush=True)
        return []

    rules: list[CustomRule] = []
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        # Tolerate older entries written before tags existed: missing
        # tags => fully wildcard (matches every route).
        entry.setdefault("id", key)
        entry.setdefault("tags", {})
        try:
            rules.append(CustomRule.model_validate(entry))
        except Exception as exc:  # pydantic ValidationError, etc.
            print(
                f"[custom-route] skipping entry {key!r} — invalid schema: {exc}",
                flush=True,
            )
    return rules


def match_rule(rule: CustomRule, route: RouteTags) -> bool:
    """Multi-facet intersection match.

    Rule axis = ``None``  => wildcard; this axis passes.
    Rule axis = list      => passes only if it intersects the route's
                             list for the same axis.
    All non-null axes must pass for the rule as a whole to match.
    """
    for axis in ("repo_type", "task_type", "change_shape"):
        rule_axis = getattr(rule.tags, axis)
        if rule_axis is None:
            continue
        route_axis = getattr(route, axis)
        if not set(rule_axis) & set(route_axis):
            return False
    return True


def select_matching_rules(
    rules: list[CustomRule], route: RouteTags
) -> list[CustomRule]:
    """Filter ``rules`` to those whose tags match the route output."""
    return [r for r in rules if match_rule(r, route)]


def format_custom_rules_for_prompt(matched: list[CustomRule]) -> str:
    """Render matched rules as a prompt-injectable text block.

    Returns "" when ``matched`` is empty so callers can omit the entire
    section. Adds the same "code wins on conflict" preamble as the
    existing ChromaDB renderer in ``src/memory/retrieve.py`` to keep the
    agent's mental model consistent across both LTM paths.
    """
    if not matched:
        return ""

    parts: list[str] = [
        "═══ CUSTOM REPAIR DISCIPLINE (TAG-ROUTED) ═══",
        "",
        "The following rules were hand-written and matched against the",
        "current case via tag routing. They are repair-discipline notes,",
        "not recipes for this specific bug.",
        "",
        "HARD CONSTRAINT: if any rule below conflicts with what the actual",
        "code in the current repo shows, the code wins.",
        "",
    ]
    for i, rule in enumerate(matched, 1):
        parts.append(f"--- Rule {i} ({rule.id}) ---")
        if rule.title:
            parts.append(f"Title: {rule.title.strip()}")
        if rule.guidance:
            parts.append("")
            parts.append("Guidance:")
            parts.append(rule.guidance.strip())
        parts.append("")
    parts.append("═══ END CUSTOM REPAIR DISCIPLINE ═══")
    return "\n".join(parts)
