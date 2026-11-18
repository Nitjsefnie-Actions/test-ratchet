"""`run`'s real artifact read by `report`: the file names between them have one test."""
import json
import os
import subprocess
import sys
import unittest

from _action_harness import ROOT
from test_run import BROWSER, LIB, LINUX, PAGE, SCRIPT, Fixtures

LIB_TEST = (b'import sys\n\nsys.path.insert(0, "src")\nimport lib\n\n\ndef test_f():\n'
            b'    assert lib.f(1)\n')


class ContractTest(Fixtures):
    def test_each_language_run_measures_is_gated_by_report(self):
        repo = self.project({'tests/test_browser.py': BROWSER, 'tests/test_lib.py': LIB_TEST,
                             'web/page.js': PAGE, 'scripts/s.sh': SCRIPT, 'src/lib.py': LIB})
        result = self.run_in(repo, cell='c', shell='scripts/*.sh', source='src', args='-n 0')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = {'python': '| python | 3 / 4 | 75.0% | 70.0 | pass |',
                'javascript': '| javascript | 2 / 2 | 100.0% | 50.0 | pass |'}
        if LINUX:
            rows['shell'] = '| shell | 2 / 3 | 66.7% | 60.0 | pass |'
        floors = {'python': 70.0, 'javascript': 50.0, 'shell': 60.0}
        (repo / '.github').mkdir()
        (repo / '.github' / 'ci-thresholds.json').write_text(json.dumps({
            'schema_version': 1, 'coverage': {language: {'floor': floors[language]}
                                              for language in rows}}), encoding='utf-8')
        summary = self.scratch / 'summary.md'
        summary.write_text('', encoding='utf-8')
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('TEST_RATCHET_', 'GITHUB_'))}
        env.update(GITHUB_STEP_SUMMARY=str(summary),
                   TEST_RATCHET_ARTIFACTS=str(self.artifact(result).parent))
        done = subprocess.run([sys.executable, '-I', str(ROOT / 'report' / 'main.py')], cwd=repo,
                              env=env, capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual([line for line in summary.read_text(encoding='utf-8').splitlines()
                          if line.startswith('| ') and 'Language' not in line],
                         list(rows.values()))


if __name__ == '__main__':
    unittest.main()
