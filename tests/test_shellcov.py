"""Bash line coverage: the measurable-line rules, the BASH_ENV hook and the conversion."""
import contextlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_ratchet import ConfigError, shellcov, shellscan
from test_split import make_repo
from _shellcov_fixtures import (BRANCHY, COINCIDENCES, EVALS, FIRST_FORK, FORKED, NEWER,
                                REWRITTEN, RULES, SUBSTITUTIONS, UNPROVABLE, VALIDATED)

LINUX = platform.system() == 'Linux'
# The PATH's bash as the hook names it in a log, and whether collect reads it by the older rule.
BASH = LINUX and subprocess.run(['bash', '-c', 'echo "${BASH_VERSINFO[0]}${BASH_VERSINFO[1]}"'],
                                capture_output=True, text=True, check=False).stdout.strip()
OLD = bool(BASH) and BASH.isdecimal() and int(BASH) < 53


def hits(name, older):
    """The expected hits of a fixture: bash 5.2's, or 5.3's where they differ."""
    return older if OLD else NEWER.get(name, older)


def quiet(function, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = function(*args)
    return result, out.getvalue()


def reported(log_dir, script):
    """The lines real bash reported for `script` in the hook's records."""
    lines = set()
    for log in Path(log_dir).iterdir():
        for record in log.read_text(encoding='utf-8').splitlines():
            source, number = record.split('\t')[-6:-4]
            if os.path.realpath(source) == os.path.realpath(script) and int(number):
                lines.add(int(number))
    return lines


class MeasurableTest(unittest.TestCase):
    def test_each_rule(self):
        for name, source, *expected in RULES:
            with self.subTest(name):
                self.assertEqual(shellscan.measurable(source, True), expected[0])
                self.assertEqual(shellscan.measurable(source), expected[-1])

    def test_an_unclosed_or_stray_construct_ends_or_skips(self):
        for source, expected in (('echo a\n[[ -n x\n', {1}), ('for x in a\n', {1}),
                                 ('echo "open\n', set()), ('cat <<E\nbody\n', set()),
                                 ('f() {\n  echo a\n  echo "open\n', {1, 2}),
                                 ('fi\n) echo a\n', {2}), ('case "$x\n', set())):
            with self.subTest(source=source):
                self.assertEqual(shellscan.measurable(source), expected)


class ArmTest(unittest.TestCase):
    def test_off_linux_nothing_is_armed_and_the_log_says_so_once(self):
        with tempfile.TemporaryDirectory() as scratch:
            for runner in ('macOS', 'Windows'):
                with self.subTest(runner=runner):
                    added, out = quiet(shellcov.arm, Path(scratch, 'logs'), {'RUNNER_OS': runner})
                    self.assertEqual(added, {})
                    self.assertEqual(out.count('\n'), 1)
                    self.assertIn(f'not measured on {runner}', out)
                    self.assertFalse(Path(scratch, 'logs').exists())

    def test_linux_arms_a_fresh_copy_of_the_hook_and_names_the_limit_once(self):
        with tempfile.TemporaryDirectory() as scratch:
            logs = Path(scratch, 'logs')
            added, out = quiet(shellcov.arm, logs, {'RUNNER_OS': 'Linux'})
            hook = Path(added['BASH_ENV'])
            try:
                self.assertEqual(set(added), {'BASH_ENV', 'TEST_RATCHET_SHELL_LOG_DIR'})
                self.assertEqual(added['TEST_RATCHET_SHELL_LOG_DIR'], str(logs))
                self.assertTrue(logs.is_dir())
                self.assertEqual(hook.read_bytes(), shellcov.HOOK.read_bytes())
                self.assertNotEqual(hook.parent, shellcov.HOOK.parent)
                self.assertEqual(out.count('\n'), 1)
                self.assertIn('cleared environment', out)
            finally:
                shutil.rmtree(hook.parent)

    def test_without_runner_os_the_platform_decides(self):
        with tempfile.TemporaryDirectory() as scratch:
            with mock.patch.object(shellcov.platform, 'system', return_value='Darwin'):
                added, out = quiet(shellcov.arm, Path(scratch, 'logs'), {})
        self.assertEqual(added, {})
        self.assertIn('not measured on Darwin', out)

    def test_an_own_bash_env_is_chained_unless_bash_would_expand_it(self):
        for own, chained in (('/etc/own-env', '/etc/own-env'), ('own-env', './own-env'),
                             ('$HOME/env', None), ('`pwd`/env', None)):
            with self.subTest(own=own), tempfile.TemporaryDirectory() as scratch:
                added, out = quiet(shellcov.arm, Path(scratch, 'logs'),
                                   {'RUNNER_OS': 'Linux', 'BASH_ENV': own})
                shutil.rmtree(Path(added['BASH_ENV']).parent)
                self.assertEqual(added.get('TEST_RATCHET_SHELL_BASH_ENV'), chained)
                self.assertEqual(f'::warning::input shell replaces BASH_ENV {own}' in out,
                                 chained is None)

    def test_a_hook_path_bash_would_expand_is_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            odd = Path(scratch, 'a$HOME')
            odd.mkdir()
            with mock.patch.object(shellcov.tempfile, 'mkdtemp', return_value=str(odd)):
                with self.assertRaisesRegex(ConfigError, 'BASH_ENV'):
                    quiet(shellcov.arm, Path(scratch, 'logs'), {'RUNNER_OS': 'Linux'})


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.scratch)
        Path(self.scratch, 'repo').mkdir()
        self.repo = make_repo(Path(self.scratch, 'repo'), {
            'bin/a.sh': b'echo a\n\necho b\n', 'bin/b.sh': b'echo c\n', 'other.sh': b'echo d\n'})
        self.logs = Path(self.scratch, 'logs')
        self.logs.mkdir()

    def collect(self, patterns='bin/*.sh'):
        return quiet(shellcov.collect, self.repo, patterns, self.logs)[0]

    def test_records_become_hits_on_measurable_lines_of_matched_files(self):
        script = os.path.join(os.path.realpath(self.repo), 'bin', 'a.sh')

        def record(line, command, function=''):
            return f'{script}\t{line}\t\t{command}\t\t{function}\n'
        Path(self.logs, '11').write_text(
            record(1, 'echo a') + record(1, 'echo a x') + record(2, 'echo a') + record(9, 'echo a')
            + record(0, 'echo a') + record(1, 'echo ab') + record(1, 'echo a', 'evaled')
            + f'{self.repo}/other.sh\t1\t\techo d\t\t\n/elsewhere/x.sh\t1\n\tnot\nno tab\n'
            f'{script}\tx\n', encoding='utf-8')
        Path(self.logs, '12').write_text(record(3, 'echo b'), encoding='utf-8')
        self.assertEqual(self.collect(), {'bin/a.sh': {1: 2, 3: 1}, 'bin/b.sh': {1: 0}})

    def test_an_unreadable_construct_is_warned_and_only_ran_lines_count_after_it(self):
        make_repo(self.repo, {'bin/c.sh': b'echo a\necho "open\necho b\n'})
        script = os.path.join(os.path.realpath(self.repo), 'bin', 'c.sh')
        Path(self.logs, '13').write_text(
            ''.join(f'{script}\t{line}\t\techo a\t\t\n' for line in (1, 2, 3, 4))
            + ''.join(f'{script}\t{line}\t\techo a\t\tdefined_below\n' for line in (3, 2, 1)),
            encoding='utf-8')
        lines, out = quiet(shellcov.collect, self.repo, 'bin/c.sh', self.logs)
        self.assertEqual(lines, {'bin/c.sh': {1: 1, 2: 2, 3: 2}})
        self.assertEqual(out, '::warning::shell coverage cannot read bin/c.sh past line 2: from'
                              ' there on only the lines bash ran are counted\n')

    def test_a_record_repeating_its_functions_entry_command_counts_in_that_log_only(self):
        make_repo(self.repo, {'bin/t.sh': b'f() {\n  echo x\n  echo y\n}\nf\n'})
        script = os.path.join(os.path.realpath(self.repo), 'bin', 't.sh')
        call = (5, 'f', '')
        logs = {'21': (call, (1, 'f', 'f'), (3, 'echo z', 'f'), (2, 'f', 'f'), call),
                '22': ((1, 'f', 'f'), (3, 'f', 'f')), '23': ((1, 'g', 'f'), (3, 'g', 'f'))}
        for log, records in logs.items():
            Path(self.logs, log).write_text('\n'.join(
                f'{script}\t{line}\t\t{command}\t\t{function}'
                for line, command, function in records), encoding='utf-8')
        Path(self.logs, '23').write_text(  # the call's own record is another file's
            f'/elsewhere/x.sh\t9\t\tg\t\t\n{Path(self.logs, "23").read_text()}', encoding='utf-8')
        self.assertEqual(self.collect('bin/t.sh'), {'bin/t.sh': {1: 3, 2: 1, 3: 1, 5: 2}})

    def test_each_log_is_read_by_the_line_rule_of_the_bash_its_name_carries(self):
        make_repo(self.repo, {'bin/m.sh': b'echo "a\nb"\n'})
        script = os.path.join(os.path.realpath(self.repo), 'bin', 'm.sh')
        for logs, expected in (({'1.52': 2}, {2: 1}), ({'1.53': 1}, {1: 1}), ({'1': 1}, {1: 1}),
                               ({}, {1: 0}), ({'1.44': 2}, {2: 1}), ({'1.52': 1}, {2: 0}),
                               ({'1.53': 2}, {1: 0}), ({'1.52': 2, '2.53': 1}, {1: 1}),
                               ({'1.53': 1, '2.52': 2}, {1: 1}), ({'1.52': 2, '2.52': 2}, {2: 2})):
            with self.subTest(logs=logs):
                shutil.rmtree(self.logs)
                self.logs.mkdir()
                for log, line in logs.items():
                    Path(self.logs, log).write_text(f'{script}\t{line}\t\techo "a\t\t\n',
                                                    encoding='utf-8')
                self.assertEqual(self.collect('bin/m.sh'), {'bin/m.sh': expected})

    def test_no_log_directory_means_not_measured(self):
        self.logs.rmdir()
        self.assertEqual(self.collect(), {})

    def test_bad_patterns_name_the_shell_input(self):
        for patterns in ('', 'nothing/*.sh', 'bin/[z-a].sh'):
            with self.subTest(patterns=patterns):
                with self.assertRaisesRegex(ConfigError, 'input shell'):
                    self.collect(patterns)
        _, out = quiet(shellcov.collect, self.repo, 'bin/b.sh\nnothing/*.sh', self.logs)
        self.assertIn('::warning::input shell patterns matched no file', out)


@unittest.skipUnless(LINUX, 'shell coverage is measured on Linux only')
class HookTest(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.scratch)
        self.logs = self.scratch / 'logs'
        added, _ = quiet(shellcov.arm, self.logs, {'RUNNER_OS': 'Linux'})
        self.addCleanup(shutil.rmtree, Path(added['BASH_ENV']).parent)
        self.env = {**os.environ, **added}

    def repo(self, files):
        (self.scratch / 'repo').mkdir()
        return make_repo(self.scratch / 'repo', files)

    def run_bash(self, script, *flags, env=None, cwd=None):
        return subprocess.run(['bash', *flags, str(script)], env=env or self.env, cwd=cwd,
                              capture_output=True, check=False)

    def test_real_bash_reports_exactly_each_rules_lines_and_each_is_credited(self):
        repo = self.repo({f'rule{index}.sh': row[1].encode() for index, row in enumerate(RULES)})
        for index, (name, _, *expected) in enumerate(RULES):
            with self.subTest(name):
                script = repo / f'rule{index}.sh'
                result = self.run_bash(script)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(reported(self.logs, script), expected[-(not OLD)])
        self.assertEqual({log.suffix for log in self.logs.iterdir()}, {f'.{BASH}'})
        lines, _ = quiet(shellcov.collect, repo, 'rule*.sh', self.logs)
        for index, (name, _, *expected) in enumerate(RULES):
            with self.subTest(name, credited=True):
                counts = lines[f'rule{index}.sh']
                self.assertEqual({line for line, count in counts.items() if count},
                                 expected[-(not OLD)])

    def test_a_python_child_measures_a_relative_script_that_changes_directory(self):
        repo = self.repo({'scripts/check.sh': BRANCHY, 'scripts/unrun.sh': b'echo x\n'})
        child = "import subprocess; subprocess.run(['bash', 'scripts/check.sh'], check=True)"
        subprocess.run([sys.executable, '-c', child], env=self.env, cwd=repo, check=True,
                       capture_output=True)
        lines, _ = quiet(shellcov.collect, repo, 'scripts/*.sh', self.logs)
        self.assertEqual(lines, {'scripts/check.sh': {2: 1, 3: 2, 4: 1, 5: 0, 7: 1, 9: 1},
                                 'scripts/unrun.sh': {1: 0}})

    @unittest.skipUnless(shutil.which('node'), 'needs node on PATH')
    def test_a_node_child_measures_the_script_through_a_symlink(self):
        repo = self.repo({'scripts/check.sh': BRANCHY})
        Path(self.scratch, 'link.sh').symlink_to(Path(repo, 'scripts/check.sh'))
        child = ("require('child_process').spawnSync('bash', [process.argv[1], 'deep'],"
                 " {stdio: 'inherit'})")
        subprocess.run(['node', '-e', child, str(self.scratch / 'link.sh')], env=self.env,
                       check=True, capture_output=True)
        lines, _ = quiet(shellcov.collect, repo, 'scripts/*.sh', self.logs)
        self.assertEqual(lines['scripts/check.sh'], {2: 1, 3: 2, 4: 1, 5: 1, 7: 0, 9: 1})

    def test_substitution_commands_credit_the_lines_they_ran_on(self):
        repo = self.repo({'s.sh': SUBSTITUTIONS})
        subprocess.run(['bash', 's.sh'], env=self.env, cwd=repo, check=True, capture_output=True)
        lines, _ = quiet(shellcov.collect, repo, 's.sh', self.logs)
        self.assertEqual(lines['s.sh'], {5: 4, 6: 1, 7: 0, 9: 2, 13: 3, 14: 4, 18: 2, 20: 3, 21: 2,
                                         25: 2, 26: 1, 27: 1, 28: 4, 29: 1, 31: 2, 32: 1, 33: 1,
                                         34: 2, 35: 1, 40: 5, 41: 1, 42: 1, 43: 1, 44: 1, 45: 1,
                                         46: 1, 47: 1, 48: 1, 49: 1, 50: 1, 51: 2, 52: 2, 54: 2,
                                         55: 1, 56: 1})

    def test_unprovable_substitution_and_eval_records_are_not_credited(self):
        repo = self.repo({'s.sh': UNPROVABLE})
        subprocess.run(['bash', 's.sh'], env=self.env, cwd=repo, check=True, capture_output=True)
        lines, _ = quiet(shellcov.collect, repo, 's.sh', self.logs)
        self.assertEqual(lines['s.sh'], hits('UNPROVABLE', {
            3: 1, 4: 1, 5: 0, 9: 1, 10: 1, 11: 0, 17: 1, 18: 1, 19: 0, 24: 1, 25: 1, 26: 0,
            31: 1, 32: 1, 33: 0, 38: 1, 39: 1, 40: 0, 44: 2, 45: 1, 46: 0, 48: 3, 49: 2, 56: 1,
            57: 0, 61: 4, 65: 3, 66: 2, 67: 1, 68: 0, 70: 1, 71: 1, 73: 1, 74: 1, 76: 1, 77: 1}))

    def test_a_fork_before_the_first_command_is_credited_at_its_reporting_line(self):
        repo = self.repo({'s.sh': FIRST_FORK})
        subprocess.run(['bash', 's.sh'], env=self.env, cwd=repo, check=True, capture_output=True)
        lines, _ = quiet(shellcov.collect, repo, 's.sh', self.logs)
        self.assertEqual(lines['s.sh'], {1: 4, 2: 3, 7: 3, 8: 1, 9: 0})

    def test_a_shifted_substitution_matching_a_line_by_chance_is_not_credited(self):
        repo = self.repo({'s.sh': COINCIDENCES})
        subprocess.run(['bash', 's.sh'], env=self.env, cwd=repo, check=True, capture_output=True)
        lines, _ = quiet(shellcov.collect, repo, 's.sh', self.logs)
        self.assertEqual(lines['s.sh'], hits(
            'COINCIDENCES', {4: 1, 5: 1, 6: 0, 7: 0, 12: 1, 13: 1, 14: 0, 15: 0}))

    def test_an_unclamped_substitution_counts_on_no_fork_it_did_not_start(self):
        repo = self.repo({'s.sh': FORKED})
        subprocess.run(['bash', 's.sh'], env=self.env, cwd=repo, check=False, capture_output=True)
        lines, _ = quiet(shellcov.collect, repo, 's.sh', self.logs)
        self.assertEqual(lines['s.sh'], {3: 1, 4: 1, 5: 0, 8: 1, 9: 1, 10: 1, 13: 1, 14: 1, 15: 0})

    def test_every_record_counts_only_where_its_line_holds_its_command(self):
        repo = self.repo({'s.sh': VALIDATED})
        subprocess.run(['bash', 's.sh'], env=self.env, cwd=repo, check=False, capture_output=True)
        lines, _ = quiet(shellcov.collect, repo, 's.sh', self.logs)
        self.assertEqual(lines['s.sh'], hits('VALIDATED', {
            1: 2, 2: 1, 3: 0, 4: 0, 5: 0, 7: 1, 10: 2, 14: 1, 15: 6, 16: 2, 18: 2, 21: 1, 22: 1,
            25: 1, 26: 1, 27: 2, 28: 1, 29: 1, 30: 2, 31: 1, 32: 1, 33: 1, 34: 1, 35: 3, 36: 2,
            37: 1, 38: 2, 39: 1, 41: 2, 42: 1, 43: 0, 44: 0, 46: 1, 47: 1, 48: 1, 49: 1, 52: 1,
            53: 1, 54: 0, 55: 0}))

    def test_rewritten_commands_count_and_no_shifted_or_foreign_entry_record_does(self):
        repo = self.repo({'s.sh': REWRITTEN})
        subprocess.run(['bash', 's.sh'], env=self.env, cwd=repo, check=True, capture_output=True)
        lines, _ = quiet(shellcov.collect, repo, 's.sh', self.logs)
        self.assertEqual(lines['s.sh'], hits('REWRITTEN', {
            1: 1, 2: 1, 3: 1, 4: 2, 5: 1, 6: 0, 7: 0, 10: 1, 11: 1, 12: 1, 13: 1, 14: 1, 16: 1,
            17: 1, 18: 2, 19: 1, 20: 0, 21: 0, 22: 0, 23: 0, 25: 0, 26: 0, 28: 2, 29: 1, 30: 2,
            31: 1, 32: 0, 35: 1, 36: 0, 37: 0, 40: 1, 42: 1, 43: 4, 44: 1, 45: 0, 47: 1, 48: 1,
            49: 1, 50: 1, 52: 2, 53: 2, 54: 0, 55: 0, 58: 1, 59: 5, 60: 1, 61: 0, 62: 0, 65: 1,
            66: 1, 67: 2, 68: 1, 69: 0, 70: 0, 71: 0, 72: 0, 73: 0, 75: 1, 76: 1, 77: 1,
            78: 3, 79: 1, 80: 1, 82: 1, 83: 1, 84: 1, 86: 1}))

    def test_after_an_eval_only_the_commands_a_line_holds_count(self):
        repo = self.repo({'s.sh': EVALS})
        subprocess.run(['bash', 's.sh'], env=self.env, cwd=repo, check=True, capture_output=True)
        lines, _ = quiet(shellcov.collect, repo, 's.sh', self.logs)
        self.assertEqual(lines['s.sh'], hits('EVALS', {
            1: 1, 3: 1, 4: 1, 6: 1, 9: 2, 10: 2, 11: 1, 12: 0, 14: 1, 15: 1, 16: 1}))

    def test_the_same_relative_source_from_two_directories_credits_each_file(self):
        repo = self.repo({'a/util.sh': b'echo a\n', 'b/util.sh': b'echo b1\necho b2\n'})
        for read in ('source', '.', 'builtin source', 'inc'):
            with self.subTest(read=read):
                shutil.rmtree(self.logs)
                self.logs.mkdir()
                Path(repo, 'main.sh').write_bytes(
                    f'inc() {{ source "$1"; }}\ncd a\n{read} ./util.sh\ncd ../b\n{read} ./util.sh\n'
                    .encode())
                subprocess.run(['bash', 'main.sh'], env=self.env, cwd=repo, check=True,
                               capture_output=True)
                lines, _ = quiet(shellcov.collect, repo, '*/util.sh', self.logs)
                self.assertEqual(lines, {'a/util.sh': {1: 1}, 'b/util.sh': {1: 1, 2: 1}})

    def test_an_own_bash_env_is_read_before_the_script_runs(self):
        own = self.scratch / 'own-env.sh'
        own.write_bytes(b'mine() { echo "mine $1"; }\necho loaded >&2\n')
        repo = self.repo({'uses.sh': b'mine one\nmine two\n'})
        added, _ = quiet(shellcov.arm, self.logs, {'RUNNER_OS': 'Linux', 'BASH_ENV': str(own)})
        self.addCleanup(shutil.rmtree, Path(added['BASH_ENV']).parent)
        runs = [subprocess.run(['bash', 'uses.sh'], env=env, cwd=repo, capture_output=True,
                               check=False)
                for env in ({**os.environ, **added}, {**os.environ, 'BASH_ENV': str(own)})]
        self.assertEqual(*[(run.returncode, run.stdout, run.stderr) for run in runs])
        self.assertEqual(runs[0].stdout, b'mine one\nmine two\n')
        lines, _ = quiet(shellcov.collect, repo, 'uses.sh', self.logs)
        self.assertEqual(lines, {'uses.sh': {1: 1, 2: 1}})

    def test_a_missing_own_bash_env_is_as_silent_as_bash_makes_it(self):
        missing = str(self.scratch / 'absent-env.sh')
        script = self.scratch / 'plain.sh'
        script.write_bytes(b'echo hi\n')
        added, _ = quiet(shellcov.arm, self.logs, {'RUNNER_OS': 'Linux', 'BASH_ENV': missing})
        self.addCleanup(shutil.rmtree, Path(added['BASH_ENV']).parent)
        runs = [self.run_bash(script, env=env)
                for env in ({**os.environ, **added}, {**os.environ, 'BASH_ENV': missing})]
        self.assertEqual(*[(run.returncode, run.stdout, run.stderr) for run in runs])
        self.assertEqual(runs[0].stdout, b'hi\n')

    def test_the_hook_holds_against_a_script_that_unsets_its_variable_or_defines_printf(self):
        plain = {key: value for key, value in self.env.items() if key != 'BASH_ENV'}
        for source in (b'unset TEST_RATCHET_SHELL_LOG_DIR\necho a\necho b\n',
                       b'printf() { echo hijack; }\necho a\nprintf x\n'):
            with self.subTest(source=source):
                shutil.rmtree(self.logs)
                self.logs.mkdir()
                script = self.scratch / 'hostile.sh'
                script.write_bytes(source)
                runs = [self.run_bash(script, env=env) for env in (self.env, plain)]
                self.assertEqual(*[(run.returncode, run.stdout, run.stderr) for run in runs])
                self.assertEqual(reported(self.logs, script), {1, 2, 3})

    def test_a_cleared_environment_is_not_measured(self):
        repo = self.repo({'scripts/check.sh': BRANCHY})
        for command, hit in ((['env', '-i', 'bash', 'scripts/check.sh'], 0),
                             (['bash', 'scripts/check.sh'], 1)):
            with self.subTest(command=command):
                subprocess.run(command, env=self.env, cwd=repo, check=True, capture_output=True)
                lines, _ = quiet(shellcov.collect, repo, 'scripts/*.sh', self.logs)
                self.assertEqual(lines['scripts/check.sh'][9], hit)

    def test_output_and_status_match_an_unhooked_run(self):
        plain = {key: value for key, value in self.env.items() if key != 'BASH_ENV'}
        # xtrace follows the pipe: a pipeline's processes trace in no fixed order.
        for name, source, flags in (
                ('errexit, xtrace and a failing command',
                 'set -e\necho "start $_"\nfalse | true\necho "pipe ${PIPESTATUS[*]}"\nset -x\n'
                 'f() { return 3; }\nf || echo "status $?"\nls /nonexistent-test-ratchet\n'
                 'echo unreachable\n', ()),
                ('nounset, errtrace and an ERR trap',
                 'set -Eu\ntrap \'echo "err $LINENO" >&2\' ERR\ng() { false; }\ng || :\n'
                 'g\necho "$_ $?"\n', ('-x',)),
                ('the first command reads $_', 'echo "$_"\n', ()),
                ('the script sets its own DEBUG trap',
                 'trap \'echo dbg >&2\' DEBUG\necho one\ntrap - DEBUG\necho two\n', ())):
            with self.subTest(name):
                script = self.scratch / 'compare.sh'
                script.write_bytes(source.encode())
                expected = self.run_bash(script, *flags, env=plain)
                for logs in ('kept', 'removed'):
                    shutil.rmtree(self.logs, ignore_errors=True)
                    if logs == 'kept':
                        self.logs.mkdir()
                    hooked = self.run_bash(script, *flags)
                    self.assertEqual((hooked.returncode, hooked.stdout, hooked.stderr),
                                     (expected.returncode, expected.stdout, expected.stderr))
                    self.assertEqual(bool(reported(self.logs, script)) if logs == 'kept'
                                     else self.logs.exists(), logs == 'kept')

    def test_a_command_string_under_nounset_keeps_its_output(self):
        plain = {key: value for key, value in self.env.items() if key != 'BASH_ENV'}
        runs = [subprocess.run(['bash', '-uc', 'echo a; false || echo "b $?"'], env=env,
                               capture_output=True, check=False) for env in (self.env, plain)]
        self.assertEqual(*[(run.returncode, run.stdout, run.stderr) for run in runs])
        self.assertEqual(runs[0].stdout, b'a\nb 1\n')

    def test_verbose_shells_echo_the_hook_once_and_are_not_measured(self):
        script = self.scratch / 'verbose.sh'
        script.write_bytes(b'echo a\necho b\n')
        plain = {key: value for key, value in self.env.items() if key != 'BASH_ENV'}
        expected = self.run_bash(script, '-v', env=plain)
        hooked = self.run_bash(script, '-v')
        self.assertEqual(hooked.stdout, expected.stdout)
        self.assertEqual(hooked.stderr, Path(self.env['BASH_ENV']).read_bytes() + expected.stderr)
        self.assertEqual(reported(self.logs, script), set())

    def test_concurrent_processes_write_whole_records(self):
        script = self.scratch / 'busy.sh'
        script.write_bytes(b'for n in 1 2 3 4; do\n  (\n    for i in $(seq 100); do\n'
                           b'      :\n    done\n  ) &\ndone\nwait\n')
        children = [subprocess.Popen(['bash', str(script)], env=self.env) for _ in range(4)]
        self.assertEqual([child.wait() for child in children], [0] * 4)
        records = [record for log in self.logs.iterdir()
                   for record in log.read_text(encoding='utf-8').split('\n')[:-1]]
        self.assertTrue(all(re.fullmatch(r'/.+\t[0-9]+\t[0-9c]*\t[^\t]*\t[^\t]*\t[^\t]*', record)
                            for record in records))
        self.assertEqual(sum(record.startswith(f'{script}\t4\t') for record in records), 1600)


if __name__ == '__main__':
    unittest.main()
