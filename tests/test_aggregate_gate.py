"""The tests workflow's aggregate folds every required leg fail-closed."""
import itertools
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'ci' / 'aggregate_gate.py'
LEGS = ('plan', 'checks', 'suites', 'report', 'shellcheck')
OK = {'result': 'success'}


def _run(payload):
    env = {key: value for key, value in os.environ.items()
           if key not in ('TEST_RATCHET_NEEDS_JSON', 'NEEDS_JSON')}
    if payload is not None:
        env['TEST_RATCHET_NEEDS_JSON'] = payload
    return subprocess.run([sys.executable, str(SCRIPT)], env=env,
                          capture_output=True, text=True, check=False)


class AggregateGateTest(unittest.TestCase):
    def test_every_leg_successful_passes(self):
        result = _run(json.dumps({leg: OK for leg in LEGS}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('All required CI legs succeeded', result.stdout)

    def test_a_leg_not_successful_fails_naming_it(self):
        for named, state in itertools.product(LEGS, ({'result': 'failure'}, {'result': 'skipped'},
                                                     {'result': 'cancelled'}, 'success', None)):
            needs = {leg: state if leg == named else OK for leg in LEGS if leg != named or state}
            with self.subTest(needs=needs):
                result = _run(json.dumps(needs))
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, f'CI legs not successful: {named}\n')

    def test_a_missing_or_malformed_payload_fails(self):
        for payload in (None, '[]', 'not json'):
            with self.subTest(payload=payload):
                result = _run(payload)
                self.assertEqual(result.returncode, 1)
                self.assertIn('CI aggregate failed', result.stderr)


if __name__ == '__main__':
    unittest.main()
