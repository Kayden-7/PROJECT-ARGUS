# -*- coding: utf-8 -*-
"""
ARGUS Test Runner -- All phases + integration
Usage: python run_tests.py
       python run_tests.py phase1        (run only phase 1)
       python run_tests.py integration   (run only integration)
"""
import subprocess
import sys
import os
import re
import time

ROOT = os.path.dirname(os.path.abspath(__file__))

SUITES = [
    ('Phase 1 -- Flask Skeleton & DB',     'tests/test_phase_1.py'),
    ('Phase 2 -- Policy Engine',           'tests/test_phase_2.py'),
    ('Phase 3 -- Approval Queue',          'tests/test_phase_3.py'),
    ('Phase 4 -- Trust Ledger',            'tests/test_phase_4.py'),
    ('Phase 5 -- Gmail Execution',         'tests/test_phase_5.py'),
    ('Phase 5b -- Message Templates',      'tests/test_templates.py'),
    ('Phase 5c -- Safety Filter + Errors', 'tests/test_safety_filter.py'),
    ('Phase 9 -- GPT-4o Agent Layer',      'tests/test_agent.py'),
    ('Integration -- Cross-Phase + Chaos', 'tests/test_integration.py'),
]

SUITE_SHORTCUTS = {
    'phase1':      0,
    'phase2':      1,
    'phase3':      2,
    'phase4':      3,
    'phase5':      4,
    'templates':   5,
    'safety':      6,
    'agent':       7,
    'integration': 8,
}

GREEN  = '\033[92m'
RED    = '\033[91m'
YELLOW = '\033[93m'
CYAN   = '\033[96m'
BOLD   = '\033[1m'
RESET  = '\033[0m'

def green(s):  return f'{GREEN}{s}{RESET}'
def red(s):    return f'{RED}{s}{RESET}'
def yellow(s): return f'{YELLOW}{s}{RESET}'
def cyan(s):   return f'{CYAN}{s}{RESET}'
def bold(s):   return f'{BOLD}{s}{RESET}'


def run_suite(label, path):
    abs_path = os.path.join(ROOT, path)
    if not os.path.exists(abs_path):
        return label, None, None, None, f'FILE NOT FOUND: {path}', None

    start = time.time()
    try:
        result = subprocess.run(
            [sys.executable, abs_path],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return label, 0, 1, 'TIMEOUT', '(test suite timed out)', time.time() - start

    elapsed = time.time() - start

    output = result.stdout
    if not output:
        output = result.stderr or '(no output)'

    match = re.search(r'RESULT:\s*(\d+) passed\s*\|\s*(\d+) failed\s*\|\s*(.+)', output)
    if match:
        passed = int(match.group(1))
        fails  = int(match.group(2))
        status = match.group(3).strip()
    else:
        passed = 0
        fails  = 1
        status = 'PARSE ERROR'

    return label, passed, fails, status, output, elapsed


def print_suite_output(label, output, failed):
    print()
    print(f'  {cyan("-" * 62)}')
    print(f'  {bold(label)}')
    print(f'  {cyan("-" * 62)}')

    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('[PASS]'):
            print(f'  {green(stripped)}')
        elif stripped.startswith('[FAIL]'):
            print(f'  {red(stripped)}')
        elif stripped.startswith('[SKIP]'):
            print(f'  {yellow(stripped)}')
        elif stripped.startswith('RESULT:'):
            colour = green if failed == 0 else red
            print(f'  {bold(colour(stripped))}')
        elif stripped.startswith('[') and stripped.endswith(']'):
            print(f'  {cyan(stripped)}')
        elif '====' in stripped or '----' in stripped:
            pass  # suppress inner banners
        else:
            print(f'  {stripped}')


def main():
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else None

    if arg and arg in SUITE_SHORTCUTS:
        suites_to_run = [SUITES[SUITE_SHORTCUTS[arg]]]
    elif arg:
        print(f'Unknown suite: {arg}')
        print(f'Options: {", ".join(SUITE_SHORTCUTS)}')
        sys.exit(1)
    else:
        suites_to_run = SUITES

    print()
    print(bold('=' * 62))
    print(bold('  ARGUS -- Full Test Suite'))
    print(bold('=' * 62))

    results = []
    for label, path in suites_to_run:
        print(f'\n  >> Running: {bold(label)} ...')
        label_r, passed, fails, status, output, elapsed = run_suite(label, path)

        if output:
            print_suite_output(label_r, output, fails if fails is not None else 1)

        if passed is None:
            results.append((label_r, 0, 1, 'ERROR', elapsed or 0))
        else:
            results.append((label_r, passed, fails, status, elapsed or 0))

    # ── Summary table ──────────────────────────────────────────────────────────
    print()
    print(bold('=' * 62))
    print(bold('  SUMMARY'))
    print(bold('=' * 62))

    col1 = max(len(r[0]) for r in results) + 2
    header = f'  {"Suite":<{col1}} {"Passed":>7} {"Failed":>7}  {"Rate":>7}  {"Time":>6}  Status'
    print(cyan(header))
    print(cyan('  ' + '-' * (col1 + 42)))

    total_passed = 0
    total_failed = 0
    any_error    = False

    for label, passed, fails, status, elapsed in results:
        total_passed += passed
        total_failed += fails
        if fails > 0:
            any_error = True

        total = passed + fails
        rate  = f'{(passed / total * 100):.1f}%' if total > 0 else 'N/A'

        p_str = green(f'{passed:>7}') if fails == 0 else f'{passed:>7}'
        f_str = red(f'{fails:>7}') if fails > 0 else green(f'{fails:>7}')
        r_str = green(f'{rate:>7}') if fails == 0 else red(f'{rate:>7}')
        t_str = f'{elapsed:.1f}s'
        s_str = green(status) if fails == 0 else red(status)
        print(f'  {label:<{col1}} {p_str} {f_str}  {r_str}  {t_str:>6}  {s_str}')

    print(cyan('  ' + '-' * (col1 + 42)))
    grand_total  = total_passed + total_failed
    grand_rate   = f'{(total_passed / grand_total * 100):.1f}%' if grand_total > 0 else 'N/A'
    total_str    = f'{total_passed:>7}'
    tfailed_str  = f'{total_failed:>7}'
    trate_str    = f'{grand_rate:>7}'
    if total_failed > 0:
        tfailed_str = red(tfailed_str)
        trate_str   = red(trate_str)
    else:
        tfailed_str = green(tfailed_str)
        total_str   = green(total_str)
        trate_str   = green(trate_str)

    print(f'  {bold("TOTAL"):<{col1+7}} {total_str} {tfailed_str}  {trate_str}')

    print()
    if not any_error:
        print(bold(green('  ALL TESTS PASSED -- safe to proceed to next phase')))
    else:
        print(bold(red(f'  {total_failed} FAILURE(S) DETECTED -- fix before proceeding')))
    print()
    print(bold('=' * 62))
    print()

    sys.exit(0 if not any_error else 1)


if __name__ == '__main__':
    main()
