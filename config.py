FREE_ACTIONS = [
    "email.compose", "email.archive", "email.mark_read",
    "email.star", "email.move", "calendar.accept",
    "calendar.label", "calendar.color", "label.apply"
]

GATED_ACTIONS = [
    "email.send.external", "email.send.internal", "email.reply",
    "email.forward", "email.delete", "calendar.create",
    "calendar.modify", "calendar.delete", "calendar.reschedule",
    "calendar.invite", "calendar.decline"
]

ALL_ACTIONS = FREE_ACTIONS + GATED_ACTIONS

PROFILE_THRESHOLDS = {
    "Strict": 101,
    "Balanced": 70,
    "Autonomous": 40
}

ACTION_CATEGORIES = {
    "email": [
        "email.compose", "email.send.external", "email.send.internal",
        "email.reply", "email.forward", "email.delete", "email.archive",
        "email.mark_read", "email.star", "email.move"
    ],
    "calendar": [
        "calendar.create", "calendar.modify", "calendar.delete",
        "calendar.label", "calendar.color", "calendar.invite",
        "calendar.accept", "calendar.decline", "calendar.reschedule"
    ],
    "label": ["label.apply"]
}

MAX_PRIME_RULES = 5
MAX_ACTIONS_PER_HOUR = 10
UNDO_WINDOW_SECONDS = 30
APPROVAL_EXPIRY_SECONDS = 300
JSON_RETRY_MAX = 3
MANUAL_REVIEW_TIMEOUT = 600
MIN_TRUST = 0.0
MAX_TRUST = 100.0

# ── Trust ledger constants ─────────────────────────────────────────────────────
STARTING_TRUST    = 40.0
INERTIA_THRESHOLD = 5
INERTIA_WEIGHT    = 0.5
OVERALL_MOD_START = 1.0
OVERALL_MOD_SHIFT = 0.05   # per-event shift to overall modifier
OVERALL_MOD_MAX   = 1.2
OVERALL_MOD_MIN   = 0.8

# Trust ceiling per profile — Balanced caps at 85 to prevent unconditional auto-approval
PROFILE_TRUST_CEILINGS = {
    "Strict":     101.0,   # unreachable — Strict always queues
    "Balanced":    85.0,
    "Autonomous": 100.0,
}

# Post-HIGH-failure damping window
DAMPING_N           = 10    # events AI must serve before damping lifts
DAMPING_MULTIPLIER  = 0.5   # success gains during damping
DAMPING_STABILITY   = 5     # consecutive successes to exit damping early

# Policy gate BLOCK carries a small negative signal (not severity-based)
POLICY_GATE_BLOCK_PENALTY = -2.0

# ── Phase 5 Part 4: safety filter — domain trust ──────────────────────────────
# Public-provider domains can NEVER be same-domain-trusted, and are barred from
# TRUSTED_DOMAINS (a stranger at gmail.com must not be auto-trusted just because
# the ARGUS account is also gmail.com).
PUBLIC_PROVIDER_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "yahoo.com", "yahoo.co.uk", "ymail.com", "icloud.com", "me.com",
    "mac.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "mail.com",
    "zoho.com", "yandex.com", "fastmail.com",
}

# An explicitly configured PRIVATE managed domain gets same-domain trust.
# The demo account is a consumer gmail.com address → None → same-domain trust OFF.
OWN_PRIVATE_DOMAIN = None

# Exact-match trusted-domain allowlist. Public providers are barred (enforced in
# safety_filter). Empty by default → every external recipient is gated.
TRUSTED_DOMAINS = set()

# ── Phase 9: GPT-4o agent layer ───────────────────────────────────────────────
import os as _os
AGENT_MODEL          = "gpt-4o"
AGENT_PROMPT_VERSION = "v1"
TAXONOMY_VERSION     = "2026-06-22"
AGENT_MAX_COMMAND_LEN = 2000
# Demo mode is process-owned server config (never a request flag). /demo/reset
# fails closed unless this is set at startup.
DEMO_MODE = _os.environ.get("ARGUS_DEMO_MODE") == "1"
# Email actions that draft a body (two-pass: extract → resolve template → draft).
DRAFTING_ACTIONS = {"email.reply", "email.send.external", "email.send.internal", "email.compose"}
