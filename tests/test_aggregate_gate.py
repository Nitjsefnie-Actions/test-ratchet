"""The tests workflow's aggregate folds every required leg fail-closed."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'ci' / 'aggregate_gate.py'


def _run(payload):
    env = {key: value for key, value in os.environ.items()
           if key not in ('TEST_RATCHET_NEEDS_JSON', 'NEEDS_JSON')}
    if payload is not None:
        env['TEST_RATCHET_NEEDS_JSON'] = payload
    return subprocess.run([sys.executable, str(SCRIPT)], env=env,
                          capture_output=True, text=True, check=False)


class AggregateGateTest(unittest.TestCase):
    def test_every_leg_successful_passes(self):
        needs = {'suites': {'result': 'success'}, 'shellcheck': {'result': 'success'}}
        result = _run(json.dumps(needs))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('All required CI legs succeeded', result.stdout)

    def test_a_leg_not_successful_fails_naming_it(self):
        for needs, named in (
                ({'suites': {'result': 'failure'}, 'shellcheck': {'result': 'success'}}, 'suites'),
                ({'suites': {'result': 'success'}, 'shellcheck': {'result': 'skipped'}},
                 'shellcheck'),
                ({'suites': {'result': 'success'}}, 'shellcheck'),
                ({'suites': 'success', 'shellcheck': {'result': 'success'}}, 'suites')):
            with self.subTest(needs=needs):
                result = _run(json.dumps(needs))
                self.assertEqual(result.returncode, 1)
                self.assertIn(named, result.stderr)

    def test_a_missing_or_malformed_payload_fails(self):
        for payload in (None, '[]', 'not json'):
            with self.subTest(payload=payload):
                result = _run(payload)
                self.assertEqual(result.returncode, 1)
                self.assertIn('CI aggregate failed', result.stderr)


if __name__ == '__main__':
    unittest.main()
