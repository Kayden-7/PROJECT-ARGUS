"""
ARGUS Phase 5 Part 4 Tests — Safety Filter + Gmail Error Handling
Run standalone: python tests/test_safety_filter.py

Three-angle: Normal + Hacker + Strict Teacher.
"""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from argus.db import init_db
import argus.safety_filter as SF
from argus.safety_filter import (parse_domain, classify_recipient, validate_trusted_domain,
                                  downgrade_reasons, apply_safety_filter)
from argus.gmail_client import classify_gmail_error
from argus.executor import _recipients_match
from googleapiclient.errors import HttpError

passed = 0
failed = 0
def sec(n): print(f'\n  [{n}]')
def check(n, cond, got=None):
    global passed, failed
    if cond: print(f'    [PASS] {n}'); passed += 1
    else:
        d = f' | got: {got}' if got is not None else ''
        print(f'    [FAIL] {n}{d}'); failed += 1

def ALLOW(): return {"decision": "ALLOW", "trace": [], "trust_impact": "pending_positive"}
def prop(action, **ent): return {"action_type": action, "entities": ent}

class _Resp:
    def __init__(self, status): self.status = status; self.reason = "x"
def http_err(status, content=b'{}'):
    return HttpError(_Resp(status), content)

class FakeG:
    def __init__(self, roles, raise_=False): self.roles = roles; self.raise_ = raise_
    def get_draft_recipients(self, draft_id):
        if self.raise_: raise RuntimeError("read fail")
        return self.roles


try:
    init_db()

    # ══ NORMAL ════════════════════════════════════════════════════════════════
    sec('Normal — safety filter downgrades flagged actions (ALLOW -> GATED)')
    r = apply_safety_filter(prop('email.delete', email_id='m1'), ALLOW())
    check('delete downgraded to GATED', r['decision'] == 'GATED')
    check('reason = SAFETY_DOWNGRADE_DELETE', r['failure_reason_code'] == 'SAFETY_DOWNGRADE_DELETE')
    check('candidate_decision preserved as ALLOW', r['candidate_decision'] == 'ALLOW')

    r = apply_safety_filter(prop('email.send.external', recipient='x@unknown.com'), ALLOW())
    check('send to unrecognized external downgraded', r['decision'] == 'GATED' and r['failure_reason_code'] == 'SAFETY_DOWNGRADE_UNRECOGNIZED_DOMAIN')

    r = apply_safety_filter(prop('email.forward', recipient='x@unknown.com'), ALLOW())
    check('external forward downgraded', r['failure_reason_code'] == 'SAFETY_DOWNGRADE_EXTERNAL_FORWARD')

    sec('Normal — non-filtered action keeps ALLOW (no over-block)')
    r = apply_safety_filter(prop('email.reply', recipient='x@unknown.com', body='hi'), ALLOW())
    check('reply (no cc/bcc) stays ALLOW', r['decision'] == 'ALLOW')

    sec('Normal — trusted-domain send is NOT downgraded')
    SF.TRUSTED_DOMAINS = {'partner.com'}
    try:
        r = apply_safety_filter(prop('email.send.external', recipient='a@partner.com'), ALLOW())
        check('send to trusted domain stays ALLOW', r['decision'] == 'ALLOW')
        check('partner.com classified TRUSTED_EXTERNAL', classify_recipient('a@partner.com') == 'TRUSTED_EXTERNAL')
    finally:
        SF.TRUSTED_DOMAINS = set()

    sec('Normal — error classifier basics')
    check('PRE_SEND 429 -> TRANSIENT', classify_gmail_error('PRE_SEND', http_err(429))['class'] == 'TRANSIENT_SAFE_TO_RETRY')
    check('PRE_SEND 400 -> PERMANENT', classify_gmail_error('PRE_SEND', http_err(400))['class'] == 'PERMANENT')
    check('SEND timeout -> UNKNOWN_DELIVERY_STATE', classify_gmail_error('SEND', RuntimeError('timeout'))['class'] == 'UNKNOWN_DELIVERY_STATE')

    # ══ HACKER ══════════════════════════════════════════════════════════════════
    sec('[HACKER] filter is one-way — never grants, never alters BLOCK/GATED')
    check('BLOCK stays BLOCK', apply_safety_filter(prop('email.delete'), {"decision": "BLOCK"})['decision'] == 'BLOCK')
    check('GATED stays GATED', apply_safety_filter(prop('email.delete'), {"decision": "GATED"})['decision'] == 'GATED')
    check('no rule + ALLOW stays ALLOW', apply_safety_filter(prop('email.reply', recipient='a@b.com', body='x'), ALLOW())['decision'] == 'ALLOW')

    sec('[HACKER] domain parsing keys off the real address')
    check('display-name trick uses real address', parse_domain('Boss <attacker@evil.com>') == 'evil.com')
    check('nested lookalike domain is external', classify_recipient('x@known.com.evil.com') == 'UNRECOGNIZED_EXTERNAL')
    check('local-part lookalike (known.com@evil.com) -> evil.com', parse_domain('known.com@evil.com') == 'evil.com')
    check('multiple addresses in one field -> MALFORMED', classify_recipient('a@b.com, c@d.com') == 'MALFORMED')
    check('empty recipient -> MALFORMED', classify_recipient('') == 'MALFORMED')

    sec('[HACKER] public providers cannot be trusted (any path)')
    check('validate_trusted_domain rejects gmail.com', validate_trusted_domain('gmail.com')['error_code'] == 'TRUSTED_DOMAIN_PUBLIC_PROVIDER_REJECTED')
    SF.TRUSTED_DOMAINS = {'gmail.com'}  # even if wrongly injected
    try:
        check('public provider stripped from effective allowlist', classify_recipient('stranger@gmail.com') == 'UNRECOGNIZED_EXTERNAL')
    finally:
        SF.TRUSTED_DOMAINS = set()
    SF.OWN_PRIVATE_DOMAIN = 'gmail.com'  # even if wrongly configured as own domain
    try:
        check('public provider never SAME_DOMAIN', classify_recipient('stranger@gmail.com') == 'UNRECOGNIZED_EXTERNAL')
    finally:
        SF.OWN_PRIVATE_DOMAIN = None

    sec('[HACKER] malformed recipient on a send -> downgrade (never auto-send)')
    r = apply_safety_filter(prop('email.send.external', recipient='not-an-email'), ALLOW())
    check('malformed send recipient downgraded', r['decision'] == 'GATED' and 'MALFORMED' in r['failure_reason_code'])

    sec('[HACKER] 429 on drafts.send is NOT assumed not-sent')
    check('SEND 429 -> UNKNOWN_DELIVERY_STATE (no duplicate-send path)',
          classify_gmail_error('SEND', http_err(429))['class'] == 'UNKNOWN_DELIVERY_STATE')

    sec('[HACKER] recipient-integrity check (role-aware)')
    check('matching To recipient -> ok', _recipients_match(FakeG({'to': ['a@b.com'], 'cc': [], 'bcc': []}), 'd', {'recipient': 'a@b.com'}))
    check('changed recipient -> mismatch', not _recipients_match(FakeG({'to': ['evil@x.com'], 'cc': [], 'bcc': []}), 'd', {'recipient': 'a@b.com'}))
    check('extra Cc -> mismatch', not _recipients_match(FakeG({'to': ['a@b.com'], 'cc': ['c@d.com'], 'bcc': []}), 'd', {'recipient': 'a@b.com'}))
    check('hidden Bcc -> mismatch', not _recipients_match(FakeG({'to': ['a@b.com'], 'cc': [], 'bcc': ['s@x.com']}), 'd', {'recipient': 'a@b.com'}))
    check('draft read failure -> fail closed (mismatch)', not _recipients_match(FakeG(None, raise_=True), 'd', {'recipient': 'a@b.com'}))

    # ══ STRICT TEACHER ══════════════════════════════════════════════════════════
    sec('[STRICT] all matching downgrade reasons preserved (not collapsed)')
    reasons = downgrade_reasons(prop('email.forward', recipient='x@unknown.com', bcc=['y@z.com']))
    check('forward+bcc yields BOTH reasons',
          'SAFETY_DOWNGRADE_EXTERNAL_FORWARD' in reasons and 'SAFETY_DOWNGRADE_BCC' in reasons, got=reasons)
    r = apply_safety_filter(prop('email.forward', recipient='x@unknown.com', bcc=['y@z.com']), ALLOW())
    check('downgrade carries reasons list', len(r['safety_downgrade_reasons']) >= 2)

    sec('[STRICT] filter is profile-independent (operates only on the ALLOW)')
    # An ALLOW is what max-trust / Autonomous produces; delete always downgrades it.
    check('delete downgrades any ALLOW regardless of how it was produced',
          apply_safety_filter(prop('email.delete', email_id='m'), ALLOW())['decision'] == 'GATED')

    sec('[STRICT] IDN canonicalization — unicode and punycode match identically')
    check('unicode domain canonicalizes to punycode', parse_domain('a@münchen.de') == 'xn--mnchen-3ya.de')
    check('punycode form canonicalizes identically', parse_domain('a@xn--mnchen-3ya.de') == 'xn--mnchen-3ya.de')

    sec('[STRICT] exact domain match only (no substring / subdomain inheritance)')
    SF.TRUSTED_DOMAINS = {'trusted.com'}
    try:
        check('exact trusted -> TRUSTED_EXTERNAL', classify_recipient('a@trusted.com') == 'TRUSTED_EXTERNAL')
        check('suffix lookalike rejected', classify_recipient('a@trusted.com.evil') == 'UNRECOGNIZED_EXTERNAL')
        check('prefix lookalike rejected', classify_recipient('a@eviltrusted.com') == 'UNRECOGNIZED_EXTERNAL')
        check('subdomain NOT inherited', classify_recipient('a@mail.trusted.com') == 'UNRECOGNIZED_EXTERNAL')
    finally:
        SF.TRUSTED_DOMAINS = set()

    sec('[STRICT] own private domain gets SAME_DOMAIN (only when non-public)')
    SF.OWN_PRIVATE_DOMAIN = 'company.com'
    try:
        check('configured private domain -> SAME_DOMAIN', classify_recipient('a@company.com') == 'SAME_DOMAIN')
        check('other domain still external', classify_recipient('a@other.com') == 'UNRECOGNIZED_EXTERNAL')
    finally:
        SF.OWN_PRIVATE_DOMAIN = None

    sec('[STRICT] error class mapping is exhaustive')
    cases = [
        ('PRE_SEND', 429, 'TRANSIENT_SAFE_TO_RETRY'),
        ('PRE_SEND', 400, 'PERMANENT'),
        ('PRE_SEND', 401, 'PERMANENT'),
        ('PRE_SEND', 404, 'PERMANENT'),
        ('PRE_SEND', 500, 'TRANSIENT_SAFE_TO_RETRY'),
        ('PRE_SEND', 418, 'UNKNOWN'),
        ('SEND', 429, 'UNKNOWN_DELIVERY_STATE'),
        ('SEND', 500, 'UNKNOWN_DELIVERY_STATE'),
        ('SEND', 404, 'UNKNOWN_DELIVERY_STATE'),
    ]
    for op, status, expected in cases:
        check(f'{op} {status} -> {expected}', classify_gmail_error(op, http_err(status))['class'] == expected)
    check('SEND result always carries a sub_reason', bool(classify_gmail_error('SEND', http_err(500))['sub_reason']))

finally:
    print()
    print('-' * 62)
    status = 'ALL CLEAR' if failed == 0 else 'FAILURES DETECTED'
    print(f'  RESULT: {passed} passed | {failed} failed | {status}')
    print('=' * 62)
    print()
    sys.exit(0 if failed == 0 else 1)
