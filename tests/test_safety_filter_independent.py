"""
Independent behavioral contract tests for argus.safety_filter.
Authored by Codex (blind to the implementation's own tests) during the Phase 5-9
quality audit. Test logic is unchanged; only the run() summary line is aligned
to run_tests.py's RESULT format.
Run with: python tests/test_safety_filter_independent.py
"""
import os
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = os.environ.get("PROJECT_ARGUS_ROOT")
if not PROJECT_ROOT:
    PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from argus import safety_filter as sf

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def require_equal(actual, expected, message=""):
    if actual != expected:
        detail = f"expected {expected!r}, got {actual!r}"
        raise AssertionError(f"{message}: {detail}" if message else detail)


@contextmanager
def patched_domains(own_private_domain=None, trusted_domains=(), public_provider_domains=None):
    old_own = sf.OWN_PRIVATE_DOMAIN
    old_trusted = sf.TRUSTED_DOMAINS
    old_public = sf.PUBLIC_PROVIDER_DOMAINS
    sf.OWN_PRIVATE_DOMAIN = own_private_domain
    sf.TRUSTED_DOMAINS = set(trusted_domains)
    if public_provider_domains is not None:
        sf.PUBLIC_PROVIDER_DOMAINS = set(public_provider_domains)
    try:
        yield
    finally:
        sf.OWN_PRIVATE_DOMAIN = old_own
        sf.TRUSTED_DOMAINS = old_trusted
        sf.PUBLIC_PROVIDER_DOMAINS = old_public


def allow_decision():
    return {"decision": "ALLOW", "decision_source": "TEST", "trace": []}


def proposal(action_type, **entities):
    return {"action_type": action_type, "entities": entities}


@test
def allow_email_delete_is_gated_with_candidate_allow():
    result = sf.apply_safety_filter(
        proposal("email.delete", email_id="msg-1"),
        allow_decision(),
    )
    require_equal(result["decision"], "GATED")
    require_equal(result["candidate_decision"], "ALLOW")
    require("SAFETY_DOWNGRADE_DELETE" in result.get("safety_downgrade_reasons", []), result)


@test
def block_and_gated_inputs_are_returned_unchanged_even_for_unsafe_actions():
    unsafe = proposal("email.delete", email_id="msg-1", bcc="hidden@example.net")
    for original in (
        {"decision": "BLOCK", "reason": "policy"},
        {"decision": "GATED", "reason": "already gated"},
    ):
        before = dict(original)
        result = sf.apply_safety_filter(unsafe, original)
        require(result is original, "non-ALLOW decisions should be returned as the same object")
        require_equal(original, before, "non-ALLOW decision content should not be modified")
        require(result.get("decision") != "ALLOW", "filter must never produce ALLOW from non-ALLOW input")


@test
def untrusted_external_send_is_gated_but_trusted_and_own_domain_sends_remain_allow():
    with patched_domains(own_private_domain="corp.example", trusted_domains={"trusted.example"}):
        untrusted = sf.apply_safety_filter(
            proposal("email.send.external", recipient="person@unknown.example", subject="Hi"),
            allow_decision(),
        )
        require_equal(untrusted["decision"], "GATED")
        require("SAFETY_DOWNGRADE_UNRECOGNIZED_DOMAIN" in untrusted.get("safety_downgrade_reasons", []), untrusted)

        trusted_decision = allow_decision()
        trusted = sf.apply_safety_filter(
            proposal("email.send.external", recipient="person@trusted.example", subject="Hi"),
            trusted_decision,
        )
        require(trusted is trusted_decision, "trusted exact-domain send should remain unchanged ALLOW")
        require_equal(trusted["decision"], "ALLOW")

        own_decision = allow_decision()
        own = sf.apply_safety_filter(
            proposal("email.send.internal", recipient="teammate@corp.example", subject="Hi"),
            own_decision,
        )
        require(own is own_decision, "configured private own-domain send should remain unchanged ALLOW")
        require_equal(own["decision"], "ALLOW")


@test
def external_forward_is_gated_even_to_trusted_external_domain():
    with patched_domains(own_private_domain="corp.example", trusted_domains={"partner.example"}):
        external = sf.apply_safety_filter(
            proposal("email.forward", recipient="ally@partner.example"),
            allow_decision(),
        )
        require_equal(external["decision"], "GATED")
        require("SAFETY_DOWNGRADE_EXTERNAL_FORWARD" in external.get("safety_downgrade_reasons", []), external)

        same_domain_decision = allow_decision()
        same_domain = sf.apply_safety_filter(
            proposal("email.forward", recipient="teammate@corp.example"),
            same_domain_decision,
        )
        require(same_domain is same_domain_decision, "same-domain forward should remain ALLOW")


@test
def any_bcc_is_gated():
    with patched_domains(own_private_domain="corp.example"):
        result = sf.apply_safety_filter(
            proposal("email.send.internal", recipient="teammate@corp.example", subject="Hi", bcc="hidden@corp.example"),
            allow_decision(),
        )
    require_equal(result["decision"], "GATED")
    require("SAFETY_DOWNGRADE_BCC" in result.get("safety_downgrade_reasons", []), result)


@test
def public_provider_domains_are_unrecognized_even_if_injected_into_trusted_domains():
    with patched_domains(trusted_domains={"gmail.com", "outlook.com", "trusted.example"}):
        require_equal(sf.classify_recipient("friend@gmail.com"), "UNRECOGNIZED_EXTERNAL")
        require_equal(sf.classify_recipient("friend@outlook.com"), "UNRECOGNIZED_EXTERNAL")
        require_equal(sf.classify_recipient("friend@trusted.example"), "TRUSTED_EXTERNAL")


@test
def name_addr_syntax_extracts_domain_and_multiple_addresses_are_malformed():
    require_equal(sf.parse_domain("Name <a@evil.com>"), "evil.com")
    require_equal(sf.classify_recipient("Name <a@evil.com>"), "UNRECOGNIZED_EXTERNAL")
    require_equal(sf.classify_recipient("a@b.com, c@d.com"), "MALFORMED")


@test
def trusted_domain_matching_is_exact_only():
    with patched_domains(trusted_domains={"x.com"}):
        require_equal(sf.classify_recipient("a@x.com"), "TRUSTED_EXTERNAL")
        require_equal(sf.classify_recipient("a@mail.x.com"), "UNRECOGNIZED_EXTERNAL")


@test
def validate_trusted_domain_rejects_public_provider():
    result = sf.validate_trusted_domain("gmail.com")
    require_equal(result.get("ok"), False)
    require_equal(result.get("error_code"), "TRUSTED_DOMAIN_PUBLIC_PROVIDER_REJECTED")


def run():
    failures = []
    for fn in TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:
            failures.append(fn.__name__)
            print(f"FAIL {fn.__name__}: {exc}")
            traceback.print_exc()
    passed = len(TESTS) - len(failures)
    print(f"RESULT: {passed} passed | {len(failures)} failed | "
          f"{'ALL CLEAR' if not failures else 'FAILURES DETECTED'}")
    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    run()
