"""
ARGUS — Safety Downgrade Filter (Phase 5 Part 4)

A 7th step AFTER the 6-layer policy hierarchy and BEFORE execution. One-way:
it can only downgrade ALLOW -> GATED; it never grants, never alters BLOCK/GATED.
Certain high-impact actions always require human approval regardless of trust.

Locked after 3 stress-test passes (✖ -> ⚠ -> ✔). Key safety properties:
- Public-provider domains (gmail.com, ...) are NEVER same-domain-trusted and are
  barred from TRUSTED_DOMAINS — a stranger at gmail.com is not auto-trusted.
- Same-domain trust only for an explicitly configured PRIVATE domain (None here).
- Domains are exact-matched after ASCII/IDNA canonicalization; malformed or
  ambiguous recipients are downgraded for human review, never guessed.
- Domain recognition is code-only — never an LLM judgment.
"""
from email.utils import parseaddr

from config import PUBLIC_PROVIDER_DOMAINS, OWN_PRIVATE_DOMAIN, TRUSTED_DOMAINS


# ── Domain parsing & classification ──────────────────────────────────────────

def parse_domain(address):
    """Canonical ASCII domain for a single address, or None if malformed/ambiguous."""
    if not address or not isinstance(address, str):
        return None
    _name, addr = parseaddr(address)
    if not addr or addr.count("@") != 1:
        return None  # zero or multiple addresses → ambiguous
    local, domain = addr.rsplit("@", 1)
    if not local.strip() or not domain.strip():
        return None
    domain = domain.strip().rstrip(".").lower()
    if not domain or " " in domain:
        return None
    try:
        return domain.encode("idna").decode("ascii")
    except Exception:
        return None  # non-ASCII / homoglyph / un-encodable → fail closed


def classify_recipient(address):
    """SAME_DOMAIN | TRUSTED_EXTERNAL | UNRECOGNIZED_EXTERNAL | MALFORMED."""
    domain = parse_domain(address)
    if domain is None:
        return "MALFORMED"
    if (OWN_PRIVATE_DOMAIN
            and domain == OWN_PRIVATE_DOMAIN.lower()
            and domain not in PUBLIC_PROVIDER_DOMAINS):
        return "SAME_DOMAIN"
    if domain in _effective_trusted_domains():
        return "TRUSTED_EXTERNAL"
    return "UNRECOGNIZED_EXTERNAL"


def _effective_trusted_domains():
    """TRUSTED_DOMAINS with any public-provider domain stripped (defense in depth)."""
    return {d.lower() for d in TRUSTED_DOMAINS if d.lower() not in PUBLIC_PROVIDER_DOMAINS}


def validate_trusted_domain(domain):
    """
    Config-time guard: a public-provider domain must never enter TRUSTED_DOMAINS.
    Returns {"ok": True, "domain": ...} or {"ok": False, "error_code": ...}.
    """
    d = (domain or "").strip().lower()
    if not d:
        return {"ok": False, "error_code": "TRUSTED_DOMAIN_EMPTY"}
    if d in PUBLIC_PROVIDER_DOMAINS:
        return {"ok": False, "error_code": "TRUSTED_DOMAIN_PUBLIC_PROVIDER_REJECTED"}
    return {"ok": True, "domain": d}


# ── The downgrade rules ───────────────────────────────────────────────────────

def downgrade_reasons(proposal):
    """All matching safety-downgrade reasons (order-preserving, de-duplicated)."""
    action = proposal.get("action_type", "")
    ent = proposal.get("entities", {}) or {}
    reasons = []

    if action == "email.delete":
        reasons.append("SAFETY_DOWNGRADE_DELETE")

    if action == "email.forward":
        cls = classify_recipient(ent.get("recipient", ""))
        if cls == "MALFORMED":
            reasons.append("SAFETY_DOWNGRADE_MALFORMED_RECIPIENT")
        elif cls != "SAME_DOMAIN":          # any external forward
            reasons.append("SAFETY_DOWNGRADE_EXTERNAL_FORWARD")

    if action in ("email.send.external", "email.send.internal"):
        cls = classify_recipient(ent.get("recipient", ""))
        if cls == "MALFORMED":
            reasons.append("SAFETY_DOWNGRADE_MALFORMED_RECIPIENT")
        elif cls == "UNRECOGNIZED_EXTERNAL":
            reasons.append("SAFETY_DOWNGRADE_UNRECOGNIZED_DOMAIN")

    # Any Bcc on any action → always human-approved.
    if ent.get("bcc"):
        reasons.append("SAFETY_DOWNGRADE_BCC")

    # A reply that introduces new Cc recipients.
    if action == "email.reply" and ent.get("cc"):
        reasons.append("SAFETY_DOWNGRADE_NEW_RECIPIENTS")

    return list(dict.fromkeys(reasons))


def apply_safety_filter(proposal, decision):
    """
    One-way: only an ALLOW decision can be downgraded to GATED. Returns a possibly
    modified decision dict carrying candidate_decision + safety_downgrade_reasons.
    """
    if not isinstance(decision, dict) or decision.get("decision") != "ALLOW":
        return decision  # never alter BLOCK / GATED; never grant

    reasons = downgrade_reasons(proposal)
    if not reasons:
        return decision

    d = dict(decision)
    trace = list(d.get("trace", []))
    trace.append({"step": "SAFETY_FILTER", "result": "DOWNGRADE",
                  "reason": f"ALLOW→GATED: {', '.join(reasons)}",
                  "before": "ALLOW", "after": "GATED"})
    d.update({
        "decision":                 "GATED",
        "decision_source":          "SAFETY_FILTER",
        "failure_type":             "SAFETY",
        "failure_reason_code":      reasons[0],
        "safety_downgrade_reasons": reasons,
        "candidate_decision":       "ALLOW",
        "terminated_at":            "SAFETY_FILTER",
        "trust_impact":             "none",
        "trace":                    trace,
        "narrative": ("Held for your approval — this action requires owner review "
                      "regardless of trust level."),
    })
    return d
