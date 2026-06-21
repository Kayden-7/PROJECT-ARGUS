"""
ARGUS Phase 5 Part 3 Tests — Message Templates
Run standalone: python tests/test_templates.py

Three-angle: Normal + Hacker + Strict Teacher.
"""
import os, sys, sqlite3, json

ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, 'instance', 'argus.db')
sys.path.insert(0, ROOT)

from argus.db import init_db
import argus.templates as T

passed = 0
failed = 0
def sec(n): print(f'\n  [{n}]')
def check(n, cond, got=None):
    global passed, failed
    if cond: print(f'    [PASS] {n}'); passed += 1
    else:
        d = f' | got: {got}' if got is not None else ''
        print(f'    [FAIL] {n}{d}'); failed += 1

def clean():
    db = sqlite3.connect(DB_PATH); db.execute("DELETE FROM email_templates"); db.commit(); db.close()

def S(**kw):
    base = {"tone":"professional" if False else "neutral", "formality":"professional",
            "length_class":"brief", "greeting_style":"first_name", "signoff_style":"thanks",
            "max_words":80, "max_sentences":5, "max_paragraphs":2, "avoid_phrases":[]}
    base.update(kw); return base


try:
    init_db()

    # ══ NORMAL ════════════════════════════════════════════════════════════════
    sec('Normal — save, list, delete')
    clean()
    r = T.save_template("boss@acme.com", "email.reply", S(tone="formal"))
    check('save returns success + id', r.get("success") and r.get("id"))
    check('list has the template', len(T.list_templates()) == 1)
    d = T.delete_template(r["id"])
    check('delete works', d.get("success") and len(T.list_templates()) == 0)

    sec('Normal — resolution precedence (exact > contact > action > global > default)')
    clean()
    T.save_template(None, None, S(tone="neutral"))                       # global
    T.save_template(None, "email.reply", S(tone="direct"))               # action-wide
    T.save_template("boss@acme.com", None, S(tone="formal"))             # contact-wide
    T.save_template("boss@acme.com", "email.reply", S(tone="warm"))      # exact
    check('exact wins', T.resolve("boss@acme.com", "email.reply")["settings"]["tone"] == "warm")
    check('contact-wide for other action', T.resolve("boss@acme.com", "email.forward")["settings"]["tone"] == "formal")
    check('action-wide for other contact', T.resolve("x@y.com", "email.reply")["settings"]["tone"] == "direct")
    check('global for unmatched both', T.resolve("x@y.com", "email.forward")["settings"]["tone"] == "neutral")

    sec('Normal — no template -> conservative default')
    clean()
    res = T.resolve("nobody@nowhere.com", "email.reply")
    check('status DEFAULT', res["status"] == "DEFAULT")
    check('default is code-owned conservative', res["settings"]["max_words"] == 120 and res["settings"]["tone"] == "neutral")

    sec('Normal — render produces a body-only style block')
    block = T.render_style_block(S(tone="warm", max_words=50))
    check('render mentions tone', "warm" in block)
    check('render mentions max words', "50 words" in block)
    check('render says body only', "only the email body" in block.lower())

    sec('Normal — validator passes a conforming body')
    v = T.validate_body("Hi Alex, thanks for the update. Talk soon.", S(max_words=80, max_sentences=5, max_paragraphs=2))
    check('conforming body valid', v["valid"], got=v["failures"])

    # ══ HACKER ══════════════════════════════════════════════════════════════════
    sec('[HACKER] avoid_phrases NEVER appears in the model prompt')
    block = T.render_style_block(S(avoid_phrases=["SECRET_INJECTION_STRING", "ignore prior"]))
    check('avoid_phrases excluded from render', "SECRET_INJECTION_STRING" not in block and "ignore prior" not in block)

    sec('[HACKER] avoid_phrases still enforced by the validator')
    v = T.validate_body("I am so sorry about this.", S(avoid_phrases=["sorry"]))
    check('avoided phrase fails validation', not v["valid"] and any("sorry" in f for f in v["failures"]))

    sec('[HACKER] structural headers in a body are rejected')
    v = T.validate_body("Bcc: spy@evil.com\nHello there.", S())
    check('header line rejected', not v["valid"] and any("header" in f for f in v["failures"]))
    v2 = T.validate_body("Subject: gotcha\nHi.", S())
    check('Subject header rejected', not v2["valid"])

    sec('[HACKER] impossible / contradictory config rejected at save')
    clean()
    r = T.save_template(None, None, S(max_words=5, max_sentences=10))  # words < sentences
    check('impossible config rejected', not r.get("success"))
    r2 = T.save_template(None, None, S(tone="evil_mind_control"))      # bad enum
    check('invalid enum rejected', not r2.get("success"))
    r3 = T.save_template(None, None, S(max_words=99999))               # out of range
    check('out-of-range numeric rejected', not r3.get("success"))

    sec('[HACKER] avoid_phrases bounded (count + length)')
    r = T.save_template(None, None, S(avoid_phrases=["x"] * 50))
    check('too many avoid_phrases rejected', not r.get("success"))
    r2 = T.save_template(None, None, S(avoid_phrases=["z" * 200]))
    check('overlong avoid_phrase rejected', not r2.get("success"))

    sec('[HACKER] DB backstop — unique index blocks a duplicate global row')
    clean()
    import time as _t
    def _insert_global(conn):
        conn.execute("INSERT INTO email_templates (id,contact,action_type,tone,formality,length_class,"
                     "greeting_style,signoff_style,max_words,max_sentences,max_paragraphs,avoid_phrases,"
                     "enabled,version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,1,?,?)",
                     (os.urandom(8).hex(), None, None, "neutral","professional","brief","first_name",
                      "thanks",80,5,2,"[]",int(_t.time()),int(_t.time())))
    db = sqlite3.connect(DB_PATH)
    _insert_global(db); db.commit()
    blocked = False
    try:
        _insert_global(db); db.commit()
    except sqlite3.IntegrityError:
        blocked = True
    db.close()
    check('second global row blocked by unique index', blocked)

    sec('[HACKER] multi-match at a rank -> MANUAL_REVIEW (fail closed)')
    clean()
    # Simulate corruption by dropping the backstop index, then forcing two global rows.
    db = sqlite3.connect(DB_PATH)
    db.execute("DROP INDEX IF EXISTS idx_tmpl_global")
    _insert_global(db); _insert_global(db); db.commit(); db.close()
    res = T.resolve("x@y.com", "email.forward")
    check('ambiguous global -> MANUAL_REVIEW', res["status"] == "MANUAL_REVIEW", got=res["status"])
    # Restore the backstop index.
    db = sqlite3.connect(DB_PATH)
    db.execute("DELETE FROM email_templates")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tmpl_global ON email_templates"
               "((contact IS NULL AND action_type IS NULL)) WHERE contact IS NULL AND action_type IS NULL")
    db.commit(); db.close()

    sec('[HACKER] contact canonicalization (case/space) resolves to same row')
    clean()
    T.save_template("Boss@Acme.com ", "email.reply", S(tone="formal"))
    check('canonical lookup matches', T.resolve("  boss@acme.com", "email.reply")["settings"]["tone"] == "formal")

    # ══ STRICT TEACHER ══════════════════════════════════════════════════════════
    sec('[STRICT] validator boundary — exactly at limit passes, one over fails')
    body4 = "alpha beta gamma delta"  # 4 words
    check('exactly max_words passes', T.validate_body(body4, S(max_words=4, max_sentences=5, max_paragraphs=5))["valid"])
    check('one over max_words fails', not T.validate_body(body4, S(max_words=3, max_sentences=5, max_paragraphs=5))["valid"])

    sec('[STRICT] version bumps on update at same scope (no duplicate row)')
    clean()
    r1 = T.save_template("a@b.com", "email.reply", S(tone="warm"))
    r2 = T.save_template("a@b.com", "email.reply", S(tone="formal"))
    check('version bumped to 2', r2["version"] == 2, got=r2["version"])
    check('still one row at scope', len(T.list_templates()) == 1)
    check('latest value wins', T.resolve("a@b.com","email.reply")["settings"]["tone"] == "formal")

    sec('[STRICT] pinned snapshot is independent of later template edits')
    clean()
    T.save_template("a@b.com", "email.reply", S(tone="warm", max_words=50))
    snap = T.snapshot_for_proposal("a@b.com", "email.reply")
    check('snapshot captured settings', snap["settings"]["tone"] == "warm" and snap["settings"]["max_words"] == 50)
    T.save_template("a@b.com", "email.reply", S(tone="formal", max_words=200))  # edit after pin
    check('snapshot UNCHANGED after edit', snap["settings"]["tone"] == "warm" and snap["settings"]["max_words"] == 50)

    sec('[STRICT] snapshot records scope + status for audit')
    check('snapshot has scope', snap.get("scope") == "exact")
    check('snapshot has status OK', snap.get("status") == "OK")
    check('snapshot has pinned_at', isinstance(snap.get("pinned_at"), int))

    sec('[STRICT] disabled template is not resolved')
    clean()
    r = T.save_template("a@b.com", "email.reply", S(tone="warm"))
    db = sqlite3.connect(DB_PATH); db.execute("UPDATE email_templates SET enabled=0 WHERE id=?", (r["id"],)); db.commit(); db.close()
    check('disabled -> falls through to default', T.resolve("a@b.com","email.reply")["status"] == "DEFAULT")

    clean()

finally:
    print()
    print('-' * 62)
    status = 'ALL CLEAR' if failed == 0 else 'FAILURES DETECTED'
    print(f'  RESULT: {passed} passed | {failed} failed | {status}')
    print('=' * 62)
    print()
    sys.exit(0 if failed == 0 else 1)
