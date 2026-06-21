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
