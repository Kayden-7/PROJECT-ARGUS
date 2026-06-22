"""
ARGUS — Gmail client (Phase 5 Part 1)

Part 1 scope: OAuth connection + a simple direct send to prove connectivity.
The crash-safe draft-based execution flow (drafts.create → atomic claim →
drafts.send → MANUAL_REVIEW on any uncertainty) is built in Part 2.

Token is stored at instance/gmail_token.json (gitignored).
Credentials (client id/secret) come from .env, never hardcoded.
"""
import os
import base64
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

ROOT = os.path.dirname(os.path.dirname(__file__))
TOKEN_PATH = os.path.join(ROOT, 'instance', 'gmail_token.json')

# Scopes cover everything Phase 5 needs:
#   gmail.compose — create + send drafts (the Part 2 crash-safe flow)
#   gmail.modify  — modify labels, trash, mark read (we use trash, never permanent delete)
SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]


def _client_config():
    """Build the OAuth client config from .env (never hardcoded)."""
    cid = os.environ.get("GOOGLE_CLIENT_ID")
    csecret = os.environ.get("GOOGLE_CLIENT_SECRET")
    redirect = os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:8081/oauth2callback")
    if not cid or not csecret:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET missing — is .env loaded?"
        )
    config = {
        "web": {
            "client_id": cid,
            "client_secret": csecret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect],
        }
    }
    return config, redirect


def build_auth_flow(state=None):
    config, redirect = _client_config()
    flow = Flow.from_client_config(config, scopes=SCOPES, state=state)
    flow.redirect_uri = redirect
    return flow


# ── Token storage ──────────────────────────────────────────────────────────────

def _save_credentials(creds):
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, 'w', encoding='utf-8') as f:
        f.write(creds.to_json())


def _load_credentials():
    """Load token from disk, refreshing it if expired. Returns None if absent."""
    if not os.path.exists(TOKEN_PATH):
        return None
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_credentials(creds)
    return creds


def save_credentials_from_flow(flow):
    creds = flow.credentials
    _save_credentials(creds)
    return creds


# ── Public helpers ───────────────────────────────────────────────────────────

def is_connected():
    try:
        creds = _load_credentials()
        return bool(creds and creds.valid)
    except Exception:
        return False


def get_service():
    creds = _load_credentials()
    if not creds or not creds.valid:
        raise RuntimeError("Gmail not connected — run the OAuth flow first.")
    return build('gmail', 'v1', credentials=creds, cache_discovery=False)


def get_connected_email():
    service = get_service()
    profile = service.users().getProfile(userId='me').execute()
    return profile.get('emailAddress')


def _raw_message(to, subject, body, sender=None):
    msg = MIMEText(body)
    msg['to'] = to
    msg['subject'] = subject
    if sender:
        msg['from'] = sender
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def send_test_email(to, subject, body):
    """
    Part 1 ONLY: a plain direct send to prove Gmail connectivity.
    Not crash-safe — that's Part 2's draft-based flow. Do not wire this
    into the execution layer; it exists to verify OAuth + send work.
    """
    service = get_service()
    raw = _raw_message(to, subject, body)
    sent = service.users().messages().send(
        userId='me', body={'raw': raw}
    ).execute()
    return {"message_id": sent.get('id'), "thread_id": sent.get('threadId')}


# ── Part 2: crash-safe primitives ────────────────────────────────────────────
# These map 1:1 to the locked execution state machine. The executor decides
# WHEN to call them; this module only knows HOW to talk to Gmail.

def get_history_id():
    """Current mailbox historyId — saved before a send as review evidence."""
    service = get_service()
    profile = service.users().getProfile(userId='me').execute()
    return profile.get('historyId')


def create_draft(to, subject, body, thread_id=None, in_reply_to=None):
    """
    Create a Gmail draft. Returns the durable draft id (stable across crashes).
    This is the pre-send checkpoint: a draft that exists but was never sent is
    a safe, recoverable state.
    """
    service = get_service()
    msg = MIMEText(body)
    msg['to'] = to
    msg['subject'] = subject
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = in_reply_to
    message = {'raw': base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread_id:
        message['threadId'] = thread_id
    draft = service.users().drafts().create(
        userId='me', body={'message': message}
    ).execute()
    return draft.get('id')


def draft_exists(draft_id):
    """True if the draft still exists (i.e. has NOT been sent/consumed)."""
    service = get_service()
    try:
        service.users().drafts().get(userId='me', id=draft_id).execute()
        return True
    except HttpError as e:
        if e.resp.status == 404:
            return False
        raise


def send_draft(draft_id):
    """
    Send an existing draft. Gmail consumes the draft and returns the new sent
    Message. Returns {message_id, thread_id}.
    """
    service = get_service()
    sent = service.users().drafts().send(
        userId='me', body={'id': draft_id}
    ).execute()
    return {"message_id": sent.get('id'), "thread_id": sent.get('threadId')}


# ── Idempotent ops (safe to replay) ──────────────────────────────────────────

def get_draft_recipients(draft_id):
    """Read a draft's recipients by role for the pre-send integrity check."""
    service = get_service()
    d = service.users().drafts().get(userId='me', id=draft_id, format='metadata').execute()
    headers = d.get('message', {}).get('payload', {}).get('headers', [])
    roles = {'to': [], 'cc': [], 'bcc': []}
    for h in headers:
        name = (h.get('name') or '').lower()
        if name in roles:
            roles[name] = [a.strip() for a in (h.get('value') or '').split(',') if a.strip()]
    return roles


# ── Error classification (Part 4) ────────────────────────────────────────────

_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded",
                 "dailyLimitExceeded", "quotaExceeded"}


def _gmail_reason(exc):
    try:
        import json as _json
        data = _json.loads(exc.content.decode("utf-8"))
        return data.get("error", {}).get("errors", [{}])[0].get("reason", "")
    except Exception:
        return ""


def classify_gmail_error(operation, exc):
    """
    Classify a Gmail failure by OPERATION + request boundary, not status alone.
    operation: 'SEND' (drafts.send invoked) | 'PRE_SEND' (create/read/validate).

    Any uncertain result AFTER invoking drafts.send is UNKNOWN_DELIVERY_STATE
    regardless of status — never assumed "not sent", never auto-retried.
    Returns {"class": ..., "sub_reason": ...}.
    """
    status = getattr(getattr(exc, "resp", None), "status", None) if isinstance(exc, HttpError) else None

    if operation == "SEND":
        sub = f"GMAIL_SEND_{status}" if status else "GMAIL_SEND_NO_RESPONSE"
        return {"class": "UNKNOWN_DELIVERY_STATE", "sub_reason": sub}

    # PRE_SEND: confirmed no send side effect yet.
    if status == 429:
        return {"class": "TRANSIENT_SAFE_TO_RETRY", "sub_reason": "GMAIL_429_RATE_LIMIT"}
    if status == 403:
        reason = _gmail_reason(exc)
        if reason in _RATE_REASONS:
            return {"class": "TRANSIENT_SAFE_TO_RETRY", "sub_reason": f"GMAIL_403_{reason}"}
        return {"class": "PERMANENT", "sub_reason": "GMAIL_403_PERMISSION"}
    if status in (400, 401, 404):
        return {"class": "PERMANENT", "sub_reason": f"GMAIL_{status}"}
    if status and 500 <= status < 600:
        return {"class": "TRANSIENT_SAFE_TO_RETRY", "sub_reason": f"GMAIL_{status}_SERVER"}
    return {"class": "UNKNOWN", "sub_reason": "GMAIL_UNMAPPED"}


def trash_message(message_id):
    """Move a message to trash (reversible, inspectable). Never permanent delete."""
    service = get_service()
    service.users().messages().trash(userId='me', id=message_id).execute()
    return {"message_id": message_id, "trashed": True}


def modify_labels(message_id, add=None, remove=None):
    """Add/remove labels — convergent, safe to replay (re-adding is a no-op)."""
    service = get_service()
    body = {}
    if add:
        body['addLabelIds'] = add
    if remove:
        body['removeLabelIds'] = remove
    service.users().messages().modify(userId='me', id=message_id, body=body).execute()
    return {"message_id": message_id, "modified": True}


# ── Frontend grounding: read-only inbox access ────────────────────────────────

def list_messages(max_results=20):
    """
    Fetch recent messages (read-only, minimal metadata for inbox UI).
    Returns [{id, subject, sender, receivedAt, snippet}].
    """
    try:
        service = get_service()
        results = service.users().messages().list(
            userId='me',
            maxResults=min(max(1, max_results), 50),
            q=''
        ).execute()
        message_ids = [m['id'] for m in results.get('messages', [])]

        messages = []
        for msg_id in message_ids:
            try:
                msg = service.users().messages().get(
                    userId='me',
                    id=msg_id,
                    format='metadata',
                    metadataHeaders=['Subject', 'From', 'Date']
                ).execute()
                headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
                messages.append({
                    'id': msg_id,
                    'subject': headers.get('Subject', '(No subject)'),
                    'sender': headers.get('From', '(Unknown)'),
                    'receivedAt': headers.get('Date', ''),
                    'snippet': msg.get('snippet', ''),
                })
            except Exception:
                continue
        return messages
    except Exception as e:
        raise RuntimeError(f"Failed to list messages: {str(e)[:100]}")


def get_message_metadata(message_id):
    """
    Fetch minimal metadata for a single message (for grounding verification).
    Returns {id, subject, sender, receivedAt}.
    Raises RuntimeError if message not found or inaccessible.
    """
    try:
        service = get_service()
        msg = service.users().messages().get(
            userId='me',
            id=message_id,
            format='metadata',
            metadataHeaders=['Subject', 'From', 'Date']
        ).execute()
        headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
        return {
            'id': message_id,
            'subject': headers.get('Subject', '(No subject)'),
            'sender': headers.get('From', '(Unknown)'),
            'receivedAt': headers.get('Date', ''),
        }
    except HttpError as e:
        if e.resp.status == 404:
            raise RuntimeError(f"Message {message_id} not found")
        raise RuntimeError(f"Cannot access message {message_id}: {str(e)[:100]}")
    except Exception as e:
        raise RuntimeError(f"Failed to fetch message metadata: {str(e)[:100]}")
