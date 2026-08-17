"""
deals.py — pipeline domain rules (Phase 4).

Stages are ordered; a deal moves forward, back, or to won/lost. Probability
defaults come from the stage but can be overridden per deal. Kept free of DB
and HTTP so it is unit-testable and shared by the router, the Pi tools and
the CSV importer.
"""

from typing import Optional

# Ordered pipeline. Configurable per install via config.yaml `deal_stages`
# (list of {id, label, probability}); these are the defaults.
DEFAULT_STAGES = [
    {"id": "lead",        "label": "Lead",        "probability": 10},
    {"id": "qualified",   "label": "Qualified",   "probability": 25},
    {"id": "proposal",    "label": "Proposal",    "probability": 50},
    {"id": "negotiation", "label": "Negotiation", "probability": 75},
    {"id": "won",         "label": "Won",         "probability": 100, "status": "won"},
    {"id": "lost",        "label": "Lost",        "probability": 0,   "status": "lost"},
]

STATUS_OPEN, STATUS_WON, STATUS_LOST = "open", "won", "lost"

# Legacy free-text clients.metadata.deal_stage values → stage ids
_LEGACY_STAGE_MAP = {
    "lead": "lead", "new": "lead", "prospect": "lead", "prospecting": "lead",
    "qualified": "qualified", "qualification": "qualified", "discovery": "qualified",
    "proposal": "proposal", "quote": "proposal", "offer": "proposal", "angebot": "proposal",
    "negotiation": "negotiation", "verhandlung": "negotiation", "contract": "negotiation",
    "won": "won", "closed won": "won", "gewonnen": "won", "customer": "won",
    "lost": "lost", "closed lost": "lost", "verloren": "lost", "churned": "lost",
}


class DealError(ValueError):
    pass


def stages() -> list[dict]:
    """Effective stage list (config override or defaults)."""
    try:
        import context
        custom = context.config.get("deal_stages")
    except Exception:
        custom = None
    out = []
    for s in (custom or DEFAULT_STAGES):
        if not isinstance(s, dict) or not s.get("id"):
            continue
        out.append({"id": str(s["id"]), "label": s.get("label") or str(s["id"]).title(),
                    "probability": int(s.get("probability", 0)),
                    "status": s.get("status") or STATUS_OPEN})
    return out or [dict(s, status=s.get("status", STATUS_OPEN)) for s in DEFAULT_STAGES]


def stage_ids() -> list[str]:
    return [s["id"] for s in stages()]


def stage_info(stage_id: str) -> Optional[dict]:
    for s in stages():
        if s["id"] == stage_id:
            return s
    return None


def status_for_stage(stage_id: str) -> str:
    s = stage_info(stage_id)
    return (s or {}).get("status", STATUS_OPEN)


def default_probability(stage_id: str) -> int:
    s = stage_info(stage_id)
    return int((s or {}).get("probability", 0))


def validate_stage(stage_id: str) -> str:
    sid = (stage_id or "").strip().lower()
    if sid not in stage_ids():
        raise DealError(f"unknown stage {stage_id!r} — valid: {', '.join(stage_ids())}")
    return sid


def normalize_legacy_stage(text: Optional[str]) -> Optional[str]:
    """Best-effort mapping of a free-text legacy deal_stage onto a stage id."""
    if not text:
        return None
    t = str(text).strip().lower()
    if t in stage_ids():
        return t
    if t in _LEGACY_STAGE_MAP:
        return _LEGACY_STAGE_MAP[t]
    for k, v in _LEGACY_STAGE_MAP.items():
        if k in t:
            return v
    return None


def parse_value(text) -> Optional[float]:
    """'€ 120.000', '120k', '1.2M', '120000' → float; None when unparseable."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().lower().replace("€", "").replace("$", "").replace("eur", "").replace("usd", "")
    s = s.replace(" ", "")
    mult = 1.0
    if s.endswith("k"):
        mult, s = 1_000.0, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000.0, s[:-1]
    # German thousands separator "120.000" vs decimal "1.5" — if more than one
    # dot or dot followed by exactly 3 digits at the end, treat dots as separators
    if s.count(".") > 1 or (s.count(".") == 1 and len(s.split(".")[1]) == 3):
        s = s.replace(".", "")
    # Comma: "1,500" / "1,500,000" (thousands) vs "12,5" (German decimal). A
    # single comma followed by exactly 3 digits at the end is a thousands sep.
    if s.count(",") == 1 and s.count(".") == 0 and len(s.split(",")[1]) != 3:
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s) * mult
    except ValueError:
        return None


def weighted_value(value: Optional[float], probability: Optional[int], stage_id: str) -> float:
    if not value:
        return 0.0
    p = probability if probability is not None else default_probability(stage_id)
    return float(value) * (p / 100.0)


def transition_check(current_status: str, new_stage: str) -> None:
    """Rules: closed deals (won/lost) can only be reopened to an open stage
    explicitly (that's allowed — deals get revived), otherwise anything goes."""
    validate_stage(new_stage)
    # No hard restrictions beyond stage validity for v1; kept as a seam.
    return None
