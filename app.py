import os
from flask import Flask, jsonify, request

# Load .env so Google OAuth credentials (client id/secret) are available.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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

    def _route_proposal(body):
        """Shared routing for a proposal dict. Returns (response_dict, status_code).
        Used by POST /api/propose and POST /api/agent/confirm."""
        from argus.kernel import kernel_entry
        from argus.queue import enqueue

        if not body or not isinstance(body, dict):
            return ({"success": False, "decision": "BLOCK",
                     "decision_dict": {"failure_reason_code": "MISSING_BODY"},
                     "queue": None, "trust": None}, 400)
        try:
            decision = kernel_entry(body)
        except Exception as e:
            return ({"success": False, "decision": "BLOCK",
                     "decision_dict": {"failure_reason_code": "INTERNAL_ERROR", "detail": str(e)},
                     "queue": None, "trust": None}, 500)

        outcome      = decision.get("decision")
        failure_type = decision.get("failure_type")

        if outcome == "BLOCK" and failure_type == "VALIDATION":
            return ({"success": True, "decision": "BLOCK",
                     "decision_dict": decision, "queue": None, "trust": None}, 400)

        if outcome == "GATED":
            queue_result = enqueue(body, decision)
            if not queue_result.get("success"):
                return ({"success": False, "decision": "BLOCK",
                         "decision_dict": {"failure_reason_code": "QUEUE_FAILURE",
                                           "detail": queue_result.get("detail", "")},
                         "queue": None, "trust": None}, 500)
            return ({"success": True, "decision": "GATED", "decision_dict": decision,
                     "queue": {"id": queue_result["id"], "expires_at": queue_result["expires_at"],
                               "status": "PENDING"}, "trust": None}, 200)

        if outcome == "ALLOW":
            from argus.trust_ledger import record_event
            from config import ALL_ACTIONS
            action_type = body.get("action_type", "")
            if action_type not in ALL_ACTIONS:
                return ({"success": True, "decision": "ALLOW",
                         "decision_dict": decision, "queue": None, "trust": None}, 200)
            tr = record_event(action_type, "SUCCESS", reason="FREE_ACTION:ALLOW")
            trust_field = {
                "event_created": tr.get("success", False), "event_id": tr.get("event_id"),
                "trust_before": tr.get("trust_before"), "trust_after": tr.get("trust_after"),
                "actual_delta": tr.get("actual_delta"),
            }
            return ({"success": True, "decision": "ALLOW",
                     "decision_dict": decision, "queue": None, "trust": trust_field}, 200)

        return ({"success": True, "decision": "BLOCK",
                 "decision_dict": decision, "queue": None, "trust": None}, 200)

    @app.route('/api/propose', methods=['POST'])
    def propose():
        resp, code = _route_proposal(request.get_json(silent=True))
        return jsonify(resp), code

    # ── Phase 9: GPT-4o agent layer ───────────────────────────────────────────

    @app.route('/api/agent/run', methods=['POST'])
    def agent_run():
        # Interprets only — never executes. Returns a canonical proposal to confirm.
        from argus.agent import run_agent
        body = request.get_json(silent=True) or {}
        command = body.get("command", "")
        try:
            result = run_agent(command)
        except Exception as e:
            return jsonify({"agent_status": "AGENT_UNAVAILABLE", "detail": str(e)}), 500
        return jsonify(result), 200

    @app.route('/api/agent/confirm', methods=['POST'])
    def agent_confirm():
        # Confirm submits the SERVER-STORED canonical proposal by id; /api/propose
        # logic re-validates and decides. Unknown/consumed id → never executes.
        from argus.agent import load_proposal, mark_consumed
        body = request.get_json(silent=True) or {}
        pid = body.get("agent_proposal_id", "")
        proposal = load_proposal(pid)
        if proposal is None:
            return jsonify({"success": False, "error_code": "PROPOSAL_NOT_FOUND",
                            "detail": "unknown, consumed, or expired proposal"}), 404
        mark_consumed(pid)
        resp, code = _route_proposal(proposal)
        return jsonify(resp), code

    @app.route('/demo/reset', methods=['POST'])
    def demo_reset():
        # Fails closed unless the process was started with ARGUS_DEMO_MODE=1.
        from config import DEMO_MODE
        if not DEMO_MODE:
            return jsonify({"success": False, "error_code": "DEMO_MODE_DISABLED",
                            "detail": "Reset is only available when the server runs in demo mode."}), 403
        from argus.demo import reset_demo
        return jsonify(reset_demo()), 200

    # ── Approval queue endpoints ───────────────────────────────────────────────

    @app.route('/api/queue')
    def queue_list():
        # expire_stale() + reconcile() run on every read — keeps queue clean and
        # drives execution forward (reconcile-on-read; no background worker).
        from argus.queue import expire_stale, fetch_pending
        expire_stale()
        try:
            from argus.executor import reconcile
            reconcile()
        except Exception:
            pass  # a reconcile failure must never break the queue read
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

    # ── Phase 5 Part 2: Execution layer (reconcile + view) ────────────────────

    @app.route('/api/executions/tick', methods=['POST'])
    def executions_tick():
        # Explicit reconcile trigger (the queue read also reconciles).
        from argus.executor import reconcile
        import sqlite3 as _sql, os as _os
        try:
            reconcile()
        except Exception as e:
            return jsonify({"success": False, "error_code": "RECONCILE_FAILED",
                            "detail": str(e)}), 500
        DB = _os.path.join(_os.path.dirname(__file__), 'instance', 'argus.db')
        db = _sql.connect(DB); db.row_factory = _sql.Row
        rows = db.execute(
            "SELECT status, COUNT(*) AS c FROM pending_executions GROUP BY status"
        ).fetchall()
        db.close()
        return jsonify({"success": True,
                        "counts": {r["status"]: r["c"] for r in rows}}), 200

    @app.route('/api/executions')
    def executions_list():
        import sqlite3 as _sql, os as _os
        DB = _os.path.join(_os.path.dirname(__file__), 'instance', 'argus.db')
        db = _sql.connect(DB); db.row_factory = _sql.Row
        rows = db.execute(
            "SELECT execution_id, approval_id, action_type, status, draft_id, "
            "message_id, status_reason, attempt_count, created_at, updated_at "
            "FROM pending_executions ORDER BY created_at DESC"
        ).fetchall()
        db.close()
        return jsonify([dict(r) for r in rows]), 200

    # ── Phase 5 Part 3: Message templates ─────────────────────────────────────

    @app.route('/api/templates')
    def templates_list():
        from argus.templates import list_templates
        return jsonify(list_templates()), 200

    @app.route('/api/templates', methods=['POST'])
    def templates_save():
        from argus.templates import save_template
        body = request.get_json(silent=True) or {}
        settings = body.get("settings", {})
        result = save_template(body.get("contact"), body.get("action_type"), settings)
        if not result.get("success"):
            return jsonify(result), 400
        return jsonify(result), 200

    @app.route('/api/templates/<template_id>', methods=['DELETE'])
    def templates_delete(template_id):
        from argus.templates import delete_template
        result = delete_template(template_id)
        return jsonify(result), (200 if result.get("success") else 404)

    @app.route('/api/templates/match')
    def templates_match():
        from argus.templates import snapshot_for_proposal
        contact = request.args.get("contact")
        action_type = request.args.get("action_type")
        return jsonify(snapshot_for_proposal(contact, action_type)), 200

    # ── Phase 5 Part 1: Gmail connection (OAuth + connectivity test) ───────────

    @app.route('/api/gmail/connect')
    def gmail_connect():
        # Local dev uses an http redirect URI; allow it for the OAuth exchange.
        os.environ.setdefault('OAUTHLIB_INSECURE_TRANSPORT', '1')
        from flask import session, redirect
        from argus.gmail_client import build_auth_flow
        flow = build_auth_flow()
        auth_url, state = flow.authorization_url(
            access_type='offline',          # request a refresh token
            include_granted_scopes='true',
            prompt='consent'                # force refresh-token issue on re-consent
        )
        session['oauth_state'] = state
        # PKCE: the code_verifier is generated here and MUST be carried to the
        # callback, or the token exchange fails with "Missing code verifier".
        session['oauth_code_verifier'] = flow.code_verifier
        return redirect(auth_url)

    @app.route('/oauth2callback')
    def oauth2callback():
        os.environ.setdefault('OAUTHLIB_INSECURE_TRANSPORT', '1')
        from flask import session
        from argus.gmail_client import (
            build_auth_flow, save_credentials_from_flow, get_connected_email
        )
        state = session.get('oauth_state')
        flow = build_auth_flow(state=state)
        # Restore the PKCE code_verifier captured during /api/gmail/connect.
        flow.code_verifier = session.get('oauth_code_verifier')
        try:
            flow.fetch_token(authorization_response=request.url)
        except Exception as e:
            return jsonify({"success": False, "error_code": "OAUTH_FAILED",
                            "detail": str(e)}), 400
        save_credentials_from_flow(flow)
        try:
            email = get_connected_email()
        except Exception:
            email = None
        return jsonify({"success": True, "connected": True, "email": email,
                        "message": "Gmail connected. You can close this tab."}), 200

    @app.route('/api/gmail/status')
    def gmail_status():
        from argus.gmail_client import is_connected, get_connected_email
        connected = is_connected()
        email = None
        if connected:
            try:
                email = get_connected_email()
            except Exception:
                connected = False
        return jsonify({"connected": connected, "email": email}), 200

    @app.route('/api/gmail/test', methods=['POST'])
    def gmail_test():
        from argus.gmail_client import is_connected, send_test_email
        if not is_connected():
            return jsonify({"success": False, "error_code": "GMAIL_NOT_CONNECTED",
                            "detail": "Connect Gmail first at /api/gmail/connect"}), 409
        body = request.get_json(silent=True) or {}
        to = body.get('to')
        if not to:
            return jsonify({"success": False, "error_code": "MISSING_RECIPIENT",
                            "detail": "Provide 'to' in the request body"}), 400
        subject = body.get('subject', 'ARGUS test email')
        message = body.get('body', 'This is a test email sent by ARGUS (Phase 5 Part 1).')
        try:
            result = send_test_email(to, subject, message)
        except Exception as e:
            return jsonify({"success": False, "error_code": "SEND_FAILED",
                            "detail": str(e)}), 502
        return jsonify({"success": True, **result}), 200

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
