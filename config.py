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
MIN_TRUST = 1.0
MAX_TRUST = 10.0
TRUST_SUCCESS = 1
TRUST_FAILURE = -3
