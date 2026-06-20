from config import ALL_ACTIONS, APPROVAL_EXPIRY_SECONDS

# Required string fields per action type
REQUIRED_FIELDS = {
    "email.send.external":  ["recipient", "subject", "body"],
    "email.send.internal":  ["recipient", "subject", "body"],
    "email.reply":          ["recipient", "body"],
    "email.forward":        ["recipient"],
    "email.delete":         ["email_id"],
    "email.compose":        ["subject", "body"],
    "email.archive":        ["email_id"],
    "email.mark_read":      ["email_id"],
    "email.star":           ["email_id"],
    "email.move":           ["email_id", "destination"],
    "calendar.create":      ["title", "start_time", "end_time"],
    "calendar.modify":      ["event_id"],
    "calendar.delete":      ["event_id"],
    "calendar.reschedule":  ["event_id", "start_time", "end_time"],
    "calendar.invite":      ["event_id", "recipient"],
    "calendar.accept":      ["event_id"],
    "calendar.decline":     ["event_id"],
    "calendar.label":       ["event_id", "label"],
    "calendar.color":       ["event_id", "color"],
    "label.apply":          ["email_id", "label"],
}

# All fields that may appear in a valid proposal
KNOWN_FIELDS = {
    "intent", "action_type", "entities", "draft", "uncertainty_flags"
}

# Entity fields that must be strings if present
STRING_ENTITY_FIELDS = {
    "recipient", "subject", "body", "email_id", "destination",
    "title", "start_time", "end_time", "event_id", "label", "color"
}

# Machine-readable reason codes
RC_UNKNOWN_ACTION    = "UNKNOWN_ACTION_TYPE"
RC_MISSING_FIELD     = "MISSING_REQUIRED_FIELD"
RC_EMPTY_FIELD       = "EMPTY_REQUIRED_FIELD"
RC_WRONG_TYPE        = "INVALID_FIELD_TYPE"
RC_INVALID_EXPIRY    = "INVALID_ACTION_EXPIRY"
RC_MISSING_ACTION    = "MISSING_ACTION_TYPE"


def _check_field(entities: dict, field: str, errors: list) -> bool:
    if field not in entities:
        errors.append({"code": RC_MISSING_FIELD, "field": field})
        return False
    value = entities[field]
    if not isinstance(value, str):
        errors.append({"code": RC_WRONG_TYPE, "field": field, "got": type(value).__name__})
        return False
    if not value.strip():
        errors.append({"code": RC_EMPTY_FIELD, "field": field})
        return False
    return True


def validate_proposal(proposal: dict) -> dict:
    errors = []
    extra_fields_logged = []

    # Strip and log unknown top-level fields
    for key in list(proposal.keys()):
        if key not in KNOWN_FIELDS:
            extra_fields_logged.append(key)
            proposal.pop(key)

    # action_type must exist and be in ALL_ACTIONS
    action_type = proposal.get("action_type")
    if not action_type:
        errors.append({"code": RC_MISSING_ACTION})
        return {"valid": False, "errors": errors, "sanitized_proposal": {}, "extra_fields_stripped": extra_fields_logged}

    if action_type not in ALL_ACTIONS:
        errors.append({"code": f"{RC_UNKNOWN_ACTION}:{action_type}"})
        return {"valid": False, "errors": errors, "sanitized_proposal": {}, "extra_fields_stripped": extra_fields_logged}

    # Validate action_expiry if present
    expiry = proposal.get("action_expiry", APPROVAL_EXPIRY_SECONDS)
    if not isinstance(expiry, int) or expiry <= 0 or expiry > 3600:
        errors.append({"code": RC_INVALID_EXPIRY, "value": expiry})

    # Validate entity fields
    entities = proposal.get("entities", {})
    if not isinstance(entities, dict):
        entities = {}

    # Type-check all string entity fields that are present
    for field in list(entities.keys()):
        if field in STRING_ENTITY_FIELDS:
            value = entities[field]
            if not isinstance(value, str):
                errors.append({"code": RC_WRONG_TYPE, "field": field, "got": type(value).__name__})
            elif not value.strip():
                errors.append({"code": RC_EMPTY_FIELD, "field": field})
        else:
            # Strip irrelevant entity fields silently
            extra_fields_logged.append(f"entities.{field}")
            entities.pop(field)

    # Check required fields for this action type — only check presence here,
    # type/empty already caught in the entity loop above
    already_checked = set(entities.keys())
    required = REQUIRED_FIELDS.get(action_type, [])
    for field in required:
        if field not in already_checked:
            _check_field(entities, field, errors)

    if errors:
        return {
            "valid": False,
            "errors": errors,
            "sanitized_proposal": {},
            "extra_fields_stripped": extra_fields_logged
        }

    sanitized = {
        "intent": proposal.get("intent", ""),
        "action_type": action_type,
        "entities": {k: v.strip() for k, v in entities.items() if isinstance(v, str)},
        "draft": proposal.get("draft", ""),
        "uncertainty_flags": proposal.get("uncertainty_flags", []),
        "action_expiry": expiry
    }

    return {
        "valid": True,
        "errors": [],
        "sanitized_proposal": sanitized,
        "extra_fields_stripped": extra_fields_logged
    }
