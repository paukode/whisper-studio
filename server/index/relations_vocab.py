"""Closed predicate vocabulary for typed entity relations.

Free-form predicates fragmented badly (49 distinct types for 98 relations in one
live DB, 113 for 323 in another — most singletons), which makes typed relations
useless for filtering or graph queries. This module pins a small, business-doc
vocabulary and normalizes the LLM's output (and legacy rows) onto it: an exact
hit is kept, a known synonym is remapped (with a direction swap where the synonym
inverts the relation), and anything else is dropped rather than kept as a vague
catch-all.
"""

from __future__ import annotations

# Canonical predicates (source -> target direction) with a one-line gloss used in
# the extraction prompt. ``related_to`` is the only allowed catch-all, and only
# when the model explicitly chooses it.
PREDICATES: dict[str, str] = {
    "works_at": "person works at organization",
    "reports_to": "person reports to their manager (a person)",
    "member_of": "person is a member of a team or organization",
    "collaborated_with": "person collaborated with person (symmetric)",
    "mentored": "person mentored person (mentor -> mentee)",
    "has_role": "person holds a role or job title",
    "hired": "organization hired a person",
    "customer_of": "org/person is a customer of a supplier organization (buyer -> seller)",
    "party_to": "person/org is a party to a contract or agreement",
    "owns": "person/org owns a product, project, or service",
    "authored": "person authored a document or report",
    "contributed_to": "person contributed to a product or project",
    "launched": "person/org launched or founded a product, org, or event",
    "uses": "person/org/product uses a technology, product, or service",
    "depends_on": "product/service depends on another product/technology/service",
    "part_of": "the smaller thing is part of the larger (team -> org, feature -> product)",
    "competitor_of": "organization competes with organization (symmetric)",
    "achieved": "person/org/product achieved a metric or result",
    "located_in": "person/org/event is located in a place",
    "attended": "person attended an event",
    "related_to": "a clearly stated connection that fits none of the above (symmetric)",
}

# Off-vocabulary predicates the LLM commonly emits -> (canonical, swap). ``swap``
# means the synonym inverts the relation, so source/target are exchanged (e.g.
# "A employs B" -> works_at(B, A); "A manages B" -> reports_to(B, A)).
INVERSE_MAP: dict[str, tuple[str, bool]] = {
    "employs": ("works_at", True),
    "works_for": ("works_at", False),
    "employed_by": ("works_at", False),
    "manages": ("reports_to", True),
    "leads": ("reports_to", True),
    "manager_of": ("reports_to", True),
    "managed_by": ("reports_to", False),
    "supervises": ("reports_to", True),
    "belongs_to": ("member_of", False),
    "works_with": ("collaborated_with", False),
    "collaborates_with": ("collaborated_with", False),
    "partnered_with": ("collaborated_with", False),
    "mentors": ("mentored", False),
    "mentored_by": ("mentored", True),
    "coached": ("mentored", False),
    "has_title": ("has_role", False),
    "role_of": ("has_role", False),
    "vendor_of": ("customer_of", True),
    "supplier_of": ("customer_of", True),
    "sells_to": ("customer_of", True),
    "buys_from": ("customer_of", False),
    "purchases_from": ("customer_of", False),
    "signed": ("party_to", False),
    "signatory_of": ("party_to", False),
    "owned_by": ("owns", True),
    "wrote": ("authored", False),
    "written_by": ("authored", True),
    "authored_by": ("authored", True),
    "created": ("launched", False),
    "founded": ("launched", False),
    "co_founded": ("launched", False),
    "used_by": ("uses", True),
    "requires": ("depends_on", False),
    "depended_on_by": ("depends_on", True),
    "contains": ("part_of", True),
    "includes": ("part_of", True),
    "competes_with": ("competitor_of", False),
    "reached": ("achieved", False),
    "attained": ("achieved", False),
    "located_at": ("located_in", False),
    "based_in": ("located_in", False),
    "has_office_in": ("located_in", False),
    "attends": ("attended", False),
    "participated_in": ("attended", False),
}


def _normalize(raw: str) -> str:
    """Lowercase, snake_case a raw predicate string for lookup."""
    s = (raw or "").strip().lower()
    for ch in (" ", "-", "/"):
        s = s.replace(ch, "_")
    return "".join(c for c in s if c.isalnum() or c == "_").strip("_")


def canonicalize_predicate(raw: str) -> tuple[str, bool] | None:
    """Map a raw predicate to ``(canonical_predicate, swap)`` or ``None`` if it
    matches nothing in the closed vocabulary. ``swap`` = exchange source/target."""
    norm = _normalize(raw)
    if not norm:
        return None
    if norm in PREDICATES:
        return (norm, False)
    return INVERSE_MAP.get(norm)


def prompt_block() -> str:
    """The predicate list rendered for the extraction system prompt."""
    return "\n".join(f"- {name}: {gloss}" for name, gloss in PREDICATES.items())
