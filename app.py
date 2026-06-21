import os
from flask import Flask, jsonify, request
from argus.db import close_db, init_db

ERROR_STATUS = {
    "ITEM_NOT_FOUND":           404,
    "INVALID_STATE_TRANSITION": 409,
    "UNDO_WINDOW_CLOSED":       409,
    "DB_ERROR":                 500,
}


def _queue_error(result):
    code = result.get("error_code", "UNKNOWN_ERROR")
    status = ERROR_STATUS.get(code, 500)
    return jsonify({"success": False, "error_code": code, "detail": result.get("detail", "")}), status


def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'argus-dev-key')

    app.teardown_appcontext(close_db)

    with app.app_context():
        init_db()

    @app.route('/health')
    def health():
        return jsonify({"status": "ok", "system": "ARGUS", "version": "1.0"})

    # ── Phase 2→3 connector ───────────────────────────────────────────────────

    @app.route('/api/propose', methods=['POST'])
    def propose():
        from argus.kernel import kernel_entry
        from argus.queue import enqueue

        body = request.get_json(silent=True)
        if not body or not isinstance(body, dict):
            return jsonify({
                "success": False, "decision": "BLOCK",
                "decision_dict": {"failure_reason_code": "MISSING_BODY"},
                "queue": None, "trust": None
            }), 400

        try:
            decision = kernel_entry(body)
        except Exception as e:
            return jsonify({
                "success": False, "decision": "BLOCK",
                "decision_dict": {"failure_reason_code": "INTERNAL_ERROR", "detail": str(e)},
                "queue": None, "trust": None
            }), 500

        outcome      = decision.get("decision")
        failure_type = decision.get("failure_type")

        # Validation BLOCK → 400 (bad input, not a policy decision)
        if outcome == "BLOCK" and failure_type == "VALIDATION":
            return jsonify({
                "success": True, "decision": "BLOCK",
                "decision_dict": decision, "queue": None, "trust": None
            }), 400

        # GATED → enqueue; if enqueue fails → fail closed as BLOCK
        if outcome == "GATED":
            queue_result = enqueue(body, decision)
            if not queue_result.get("success"):
                return jsonify({
                    "success": False, "decision": "BLOCK",
                    "decision_dict": {"failure_reason_code": "QUEUE_FAILURE",
                                      "detail": queue_result.get("detail", "")},
                    "queue": None, "trust": None
                }), 500
            return jsonify({
                "success": True, "decision": "GATED",
                "decision_dict": decision,
                "queue": {
                    "id":         queue_result["id"],
                    "expires_at": queue_result["expires_at"],
                    "status":     "PENDING"
                },
                "trust": None
            }), 200

        # ALLOW → Phase 2→4 connector: record trust event for FREE action
        # FREE actions are executed immediately on ALLOW (no separate execution phase),
        # so trust writes here correctly reflect actual execution outcome.
        if outcome == "ALLOW":
            from argus.trust_ledger import record_event
            from config import ALL_ACTIONS
            action_type = body.get("action_type", "")
            if action_type not in ALL_ACTIONS:
                return jsonify({
                    "success": True, "decision": "ALLOW",
                    "decision_dict": decision, "queue": None, "trust": None
                }), 200
            trust_result = record_event(action_type, "SUCCESS", reason="FREE_ACTION:ALLOW")
            trust_field = {
                "event_created": trust_result.get("success", False),
                "event_id":      trust_result.get("event_id"),
                "trust_before":  trust_result.get("trust_before"),
                "trust_after":   trust_result.get("trust_after"),
                "actual_delta":  trust_result.get("actual_delta"),
            }
            return jsonify({
                "success": True, "decision": "ALLOW",
                "decision_dict": decision, "queue": None, "trust": trust_field
            }), 200

        # Policy BLOCK (Prime Rule, Hard Stop, Gate) → 200
        return jsonify({
            "success": True, "decision": "BLOCK",
            "decision_dict": decision, "queue": None, "trust": None
        }), 200

    # ── Approval queue endpoints ───────────────────────────────────────────────

    @app.route('/api/queue')
    def queue_list():
        # expire_stale() runs on every read — intentional, keeps queue clean
        from argus.queue import expire_stale, fetch_pending
        expire_stale()
        items = fetch_pending()
        return jsonify(items), 200

    @app.route('/api/queue/<item_id>')
    def queue_detail(item_id):
        import sqlite3, os as _os
        DATABASE = _os.path.join(_os.path.dirname(__file__), 'instance', 'argus.db')
        try:
            db = sqlite3.connect(DATABASE)
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM approval_queue WHERE id=?", (item_id,)
            ).fetchone()
            db.close()
        except Exception as e:
            return jsonify({"success": False, "error_code": "DB_ERROR", "detail": str(e)}), 500

        if not row:
            return jsonify({"success": False, "error_code": "ITEM_NOT_FOUND", "detail": ""}), 404

        return jsonify(dict(row)), 200

    @app.route('/api/queue/<item_id>/approve', methods=['POST'])
    def queue_approve(item_id):
        from argus.queue import approve
        result = approve(item_id)
        if not result.get("success"):
            return _queue_error(result)

        # Trust SUCCESS fires in Phase 5 after actual Gmail execution, not at approval time.
        # APPROVED = human consent, not execution success. Trust must reflect reliability, not approvals.
        return jsonify({"success": True, "id": result["id"], "status": result["status"],
                        "approved_at": result["approved_at"]}), 200

    @app.route('/api/queue/<item_id>/reject', methods=['POST'])
    def queue_reject(item_id):
        from argus.queue import reject
        body = request.get_json(silent=True) or {}
        reason = body.get("reason", "")
        if not isinstance(reason, str) or not reason.strip():
            return jsonify({"success": False, "error_code": "MISSING_REASON",
                            "detail": "reason must be a non-empty string"}), 400
        result = reject(item_id, reason.strip())
        if not result.get("success"):
            return _queue_error(result)

        # Human rejection ≠ agent failure. A user changing their mind must not penalise trust.
        # Trust FAILURE fires in Phase 5 only on actual execution failure (Gmail API error / timeout).
        return jsonify({"success": True, "id": result["id"], "status": result["status"]}), 200

    @app.route('/api/queue/<item_id>/cancel', methods=['POST'])
    def queue_cancel(item_id):
        from argus.queue import cancel
        result = cancel(item_id)
        if not result.get("success"):
            return _queue_error(result)
        return jsonify({"success": True, "id": result["id"], "status": result["status"]}), 200

    # ── Phase 4 Part 3: Trust read endpoint ───────────────────────────────────

    @app.route('/api/trust/<action_type>')
    def trust_get(action_type):
        from config import ALL_ACTIONS
        from argus.trust_ledger import get_trust
        if action_type not in ALL_ACTIONS:
            return jsonify({
                "success":     False,
                "error_code":  "UNKNOWN_ACTION_TYPE",
                "detail":      f"{action_type} is not a recognised action type",
                "valid_actions": ALL_ACTIONS,
            }), 404
        t = get_trust(action_type)
        return jsonify({"success": True, **t}), 200

    return app


app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8081, debug=True)
