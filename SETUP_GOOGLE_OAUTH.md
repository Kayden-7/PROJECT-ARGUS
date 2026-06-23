# Google OAuth & Environment Setup

If you cloned this repo and can't find `.env`, `credentials.json`, or the Gmail
token — that's **intentional**. Those files contain secrets (OAuth client secret,
API keys, live access tokens) and are gitignored so they never reach GitHub.
This guide tells you how to supply your own.

---

## TL;DR for frontend work — you probably don't need any of this

Baldwin / frontend devs: the UI is built against a **fake login** and does not
require real Google or OpenAI keys.

- **Username:** `PROJECT_ARGUS`
- **Password:** `ARGUS_DEMO`

See `FRONTEND_HANDOFF.md`. You only need the steps below if you want to run the
**live backend** with real Gmail sending + GPT proposals.

---

## What the backend needs (two files, both gitignored)

| File | Purpose | Template in repo |
|------|---------|------------------|
| `.env` | Client ID/secret, redirect URI, Flask `SECRET_KEY`, `OPENAI_API_KEY` | `.env.example` |
| `credentials.json` | Google OAuth client (web) config | `credentials.json.example` |

A third file, `instance/gmail_token.json`, is **created automatically** the first
time you complete the OAuth consent flow — you don't make it by hand.

---

## Step 1 — Create a Google OAuth client

1. Go to <https://console.cloud.google.com/> → create/select a project.
2. **APIs & Services → Library** → enable **Gmail API**.
3. **APIs & Services → OAuth consent screen** → External → add your Google
   account under **Test users** (required while the app is unverified).
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Web application**
   - **Authorized redirect URI:** `http://localhost:8081/oauth2callback`
     (must match `GOOGLE_REDIRECT_URI` exactly)
5. **Download JSON** → save as `credentials.json` in the repo root.

## Step 2 — Fill in `.env`

```bash
cp .env.example .env
cp credentials.json.example credentials.json   # then paste your downloaded JSON
```

Edit `.env` with the values from your OAuth client. Generate a Flask secret:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Add your OpenAI key (<https://platform.openai.com/api-keys>) as `OPENAI_API_KEY`
if you want live GPT proposals (Phase 9).

## Step 3 — Connect Gmail

Run the backend, then hit the connect endpoint and complete the Google consent
screen. ARGUS writes the refresh token to `instance/gmail_token.json`
automatically. Required scopes (already set in `argus/gmail_client.py`):

- `gmail.compose` — create + send drafts
- `gmail.modify` — labels / trash / mark-read (ARGUS trashes, never hard-deletes)

---

## Getting the real keys (for teammates on THIS project)

The actual secrets are **not** shared via GitHub — ever. If you need the real
project credentials (not your own), get them from Kayden through a secure channel
(password manager share / encrypted message / in person), not a commit, not chat.
