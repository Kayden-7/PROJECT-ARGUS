import os
import hmac
from flask import Flask, jsonify, request, send_from_directory

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


FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'frontend')

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _control_authorized(req):
    """Gate for privileged control-plane POSTs (emergency stop, etc.).

    If ARGUS_CONTROL_TOKEN is set: require a matching X-Control-Token
    (constant-time compare); if an Origin is present it must match
    ARGUS_CANONICAL_ORIGIN when that is configured. If the token is UNSET, this
    is local-operator control only — accept solely loopback callers. This is
    NOT authenticated owner-only access; a public deploy must set the token.
    """
    token = os.environ.get("ARGUS_CONTROL_TOKEN")
    if token:
        supplied = req.headers.get("X-Control-Token", "")
        if not hmac.compare_digest(supplied, token):
            return False
        origin = req.headers.get("Origin")
        canonical = os.environ.get("ARGUS_CANONICAL_ORIGIN")
        if origin and canonical and origin != canonical:
            return False
        return True
    # No token configured -> loopback-only local-operator control.
    return (req.remote_addr or "") in _LOOPBACK


def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'argus-dev-key')

    app.teardown_appcontext(close_db)

    with app.app_context():
        init_db()

    # No CORS headers: the frontend is served same-origin by this app, so JSON
    # POSTs need neither CORS nor a preflight. A wildcard Access-Control-Allow-
    # Origin would let any site call the control plane — removed deliberately.

    # Scoped routes (not a blanket /<path:filename> catch-all) so an unmatched
    # path like /api/nonexistent still 404s instead of colliding with the
    # static rule's method set and surfacing as 405.
    @app.route('/')
    def index():
        return send_from_directory(FRONTEND_DIR, 'index.html')

    @app.route('/js/<path:filename>')
    def frontend_js(filename):
        return send_from_directory(os.path.join(FRONTEND_DIR, 'js'), filename)

    @app.route('/css/<path:filename>')
    def frontend_css(filename):
        return send_from_directory(os.path.join(FRONTEND_DIR, 'css'), filename)

    @app.route('/health')
    def health():
        return jsonify({"status": "ok", "system": "ARGUS", "version": "1.0"})

    # ── Phase 8 Part 2: emergency stop (hard stop + epoch) ─────────────────────
    @app.route('/api/system/emergency-stop', methods=['GET'])
    def emergency_stop_status():
        from argus import kernel
        st = kernel.hard_stop_status()
        return jsonify({"success": st.get("ok", True), **st}), 200

    @app.route('/api/system/emergency-stop', methods=['POST'])
    def emergency_stop_set():
        from argus import kernel
        if not _control_authorized(request):
            return jsonify({"success": False, "error_code": "UNAUTHORIZED",
                            "detail": "control-plane access denied"}), 403
        body = request.get_json(silent=True) or {}
        engaged = body.get("engaged")
        if not isinstance(engaged, bool):
            return jsonify({"success": False, "error_code": "INVALID_ENGAGED",
                            "detail": "engaged must be a boolean"}), 400
        reason = body.get("reason")
        result = kernel.set_hard_stop(engaged, updated_by="control", reason=reason)
        if not result.get("success"):
            code = result.get("error_code", "HARD_STOP_FAILED")
            http = (400 if code in ("INVALID_ENGAGED", "INVALID_REASON", "REJECTION_REASON_TOO_LONG")
                    else 503 if code in ("BUSY", "STATE_UNAVAILABLE") else 500)
            return jsonify({"success": False, "error_code": code,
                            "detail": result.get("detail", "")}), http
        return jsonify({"success": True, "engaged": result["engaged"],
                        "epoch": result["epoch"], "transitioned": result["transitioned"]}), 200

    # ── Execution delay (Settings > Execution Delay) ───────────────────────────
    @app.route('/api/system/execution-delay', methods=['GET'])
    def execution_delay_get():
        from argus import kernel
        result = kernel.get_execution_delay()
        if not result.get("success"):
            return jsonify(result), 500
        return jsonify(result), 200

    @app.route('/api/system/execution-delay', methods=['POST'])
    def execution_delay_set():
        from argus import kernel
        if not _control_authorized(request):
            return jsonify({"success": False, "error_code": "UNAUTHORIZED",
                            "detail": "control-plane access denied"}), 403
        body = request.get_json(silent=True) or {}
        seconds = body.get("seconds")
        if not isinstance(seconds, int) or isinstance(seconds, bool):
            return jsonify({"success": False, "error_code": "INVALID_SECONDS",
                            "detail": "seconds must be an integer"}), 400
        result = kernel.set_execution_delay(seconds, updated_by="control")
        if not result.get("success"):
            return jsonify(result), 500
        return jsonify(result), 200

    # ── Phase 2→3 connector ───────────────────────────────────────────────────

    def _audit_decision(correlation, body, decision):
        from argus.audit import safe_record
        from argus.safety_filter import classify_recipient
        recipient = (body.get("entities", {}) or {}).get("recipient", "")
        payload = {
            "action_type": body.get("action_type"),
            "recipient_scope": classify_recipient(recipient) if recipient else None,
            "candidate_decision": decision.get("candidate_decision", decision.get("decision")),
            "final_outcome": decision.get("decision"),
            "decision_source": decision.get("decision_source"),
            "failure_reason_code": decision.get("failure_reason_code"),
            "safety_downgrade_reasons": decision.get("safety_downgrade_reasons", []),
            "trust_at_evaluation": decision.get("trust_at_evaluation"),
        }
        safe_record("DECISION_EVALUATED", correlation_id=correlation,
                    idempotency_key=f"{correlation}:DECISION",
                    action_type=body.get("action_type"), outcome=decision.get("decision"),
                    reason=decision.get("failure_reason_code"), payload=payload)

    def _route_proposal(body):
        """Shared routing for a proposal dict. Returns (response_dict, status_code).
        Used by POST /api/propose and POST /api/agent/confirm."""
        import uuid as _uuid
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
            # correlation = queue id → links decision ↔ queue transitions ↔ execution.
            _audit_decision(queue_result["id"], body, decision)
            return ({"success": True, "decision": "GATED", "decision_dict": decision,
                     "queue": {"id": queue_result["id"], "expires_at": queue_result["expires_at"],
                               "status": "PENDING"}, "trust": None}, 200)

        _audit_decision(str(_uuid.uuid4()), body, decision)
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
        # Raw deterministic-policy API: still fully governed by policy + safety +
        # trust (kernel_entry). Phase 8 admission (dedup + rate limit) is enforced
        # on the agent proposal-creation path (run_agent), not here — this is the
        # engine surface used by tests, not an AI action. (Hardening deferred.)
        resp, code = _route_proposal(request.get_json(silent=True))
        return jsonify(resp), code

    # ── Phase 9: GPT-4o agent layer ───────────────────────────────────────────

    @app.route('/api/agent/run', methods=['POST'])
    def agent_run():
        # Interprets only — never executes. Returns a canonical proposal to confirm.
        # Optional: selected_email_id for frontend grounding (verifies email exists).
        from argus.agent import run_agent
        body = request.get_json(silent=True) or {}
        command = body.get("command", "")
        selected_email_id = body.get("selected_email_id")
        try:
            result = run_agent(command, selected_email_id=selected_email_id)
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

    # ── Phase 7: Audit trail ──────────────────────────────────────────────────

    @app.route('/api/audit')
    def audit_list():
        from argus.audit import recent
        return jsonify(recent(request.args.get("limit", 100))), 200

    @app.route('/api/audit/verify')
    def audit_verify():
        from argus.audit import verify_chain
        return jsonify(verify_chain()), 200

    @app.route('/api/audit/summary')
    def audit_summary():
        from argus.audit import summary
        return jsonify(summary(request.args.get("since", 0))), 200

    @app.route('/api/audit/replay/<correlation_id>')
    def audit_replay(correlation_id):
        from argus.audit import replay
        return jsonify(replay(correlation_id)), 200

    @app.route('/api/trust/<action_type>/history')
    def trust_history(action_type):
        from config import ALL_ACTIONS
        import sqlite3 as _sql, os as _os
        if action_type not in ALL_ACTIONS:
            return jsonify({"success": False, "error_code": "UNKNOWN_ACTION_TYPE"}), 404
        DB = _os.path.join(_os.path.dirname(__file__), 'instance', 'argus.db')
        db = _sql.connect(DB); db.row_factory = _sql.Row
        rows = db.execute(
            "SELECT timestamp, delta, reason, resulting_trust FROM trust_events "
            "WHERE action_type=? ORDER BY timestamp ASC, event_id ASC", (action_type,)
        ).fetchall()
        db.close()
        points, prev = [], None
        for r in rows:
            points.append({"timestamp": r["timestamp"], "previous_trust": prev,
                           "delta": r["delta"], "resulting_trust": r["resulting_trust"],
                           "reason": r["reason"]})
            prev = r["resulting_trust"]
        return jsonify({"action_type": action_type, "stepped": True, "points": points}), 200

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
        # Reconcile-on-read (same pattern as GET /api/queue) — without this,
        # nothing advances until something explicitly calls tick, which is
        # why approved items used to sit invisible until a manual "Process
        # now" click long after the delay had already elapsed.
        from argus.queue import expire_stale
        from argus.executor import reconcile
        from argus.kernel import get_execution_delay
        import sqlite3 as _sql, os as _os, json as _json
        expire_stale()
        try:
            reconcile()
        except Exception:
            pass
        DB = _os.path.join(_os.path.dirname(__file__), 'instance', 'argus.db')
        db = _sql.connect(DB); db.row_factory = _sql.Row
        rows = db.execute(
            "SELECT execution_id, approval_id, action_type, status, draft_id, "
            "message_id, status_reason, attempt_count, created_at, updated_at "
            "FROM pending_executions ORDER BY created_at DESC"
        ).fetchall()
        executions = [dict(r) for r in rows]
        promoted_approval_ids = {e["approval_id"] for e in executions if e["approval_id"]}

        # Approved items not yet promoted (still inside the execution delay)
        # have no pending_executions row at all — surface them as a synthetic
        # SCHEDULED entry with a real countdown, instead of being invisible
        # until promote_approved() finally picks them up.
        delay = get_execution_delay()
        undo_seconds = delay["seconds"] if delay.get("success") else 60
        approved_rows = db.execute(
            "SELECT id, proposal_json, approved_at, created_at FROM approval_queue WHERE status='APPROVED'"
        ).fetchall()
        for r in approved_rows:
            if r["id"] in promoted_approval_ids:
                continue
            try:
                action_type = _json.loads(r["proposal_json"]).get("action_type", "(unknown)")
            except Exception:
                action_type = "(unknown)"
            executions.append({
                "execution_id": None, "approval_id": r["id"], "action_type": action_type,
                "status": "SCHEDULED", "draft_id": None, "message_id": None,
                "status_reason": None, "attempt_count": 0,
                "created_at": r["created_at"], "updated_at": r["approved_at"],
                "execute_at": (r["approved_at"] or 0) + undo_seconds,
            })
        db.close()
        executions.sort(key=lambda e: e.get("created_at") or 0, reverse=True)
        return jsonify(executions), 200

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

    @app.route('/api/gmail/messages')
    def gmail_messages():
        # Read-only inbox list for frontend email selection.
        # Returns [{id, subject, sender, receivedAt, snippet}].
        from argus.gmail_client import is_connected, list_messages
        if not is_connected():
            return jsonify({"success": False, "error_code": "GMAIL_NOT_CONNECTED",
                            "detail": "Connect Gmail first at /api/gmail/connect"}), 409
        limit = request.args.get('limit', '20')
        try:
            limit = max(1, min(int(limit), 50))
        except ValueError:
            limit = 20
        try:
            messages = list_messages(max_results=limit)
            return jsonify({"success": True, "messages": messages}), 200
        except Exception as e:
            return jsonify({"success": False, "error_code": "FETCH_FAILED",
                            "detail": str(e)[:200]}), 503

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
    # Replit (and most PaaS) inject the port via $PORT; fall back to 8081 locally.
    port = int(os.environ.get('PORT', 8081))
    app.run(host='0.0.0.0', port=port, debug=True)
