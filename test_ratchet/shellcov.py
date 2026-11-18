"""Bash line coverage on Linux: a BASH_ENV hook logs every command bash runs, and the logs
become line hits for the tracked scripts the `shell` globs match."""
import os
import platform
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple, Union

from test_ratchet import ConfigError, ghio, paths, split
from test_ratchet.cobertura import Lines
from test_ratchet.shellscan import Scan

HOOK = Path(__file__).with_name('shellcov_hook.sh')
LOG_DIR = 'TEST_RATCHET_SHELL_LOG_DIR'
OWN_BASH_ENV = 'TEST_RATCHET_SHELL_BASH_ENV'  # read by the hook before it arms


def arm(log_dir: Union[str, Path], env: Mapping[str, str]) -> Dict[str, str]:
    """The environment additions that measure the bash children of a step; {} off Linux."""
    runner = env.get('RUNNER_OS') or platform.system()
    if runner != 'Linux':
        ghio.notice(f'shell scripts are not measured on {runner}: shell coverage is Linux only')
        return {}
    hook = os.path.join(tempfile.mkdtemp(prefix='test-ratchet-shell-'), 'hook.sh')
    if any(char in hook for char in '$`\\'):
        raise ConfigError(f'cannot arm shell coverage: bash would expand the BASH_ENV path {hook}')
    shutil.copyfile(HOOK, hook)
    os.makedirs(log_dir, exist_ok=True)
    added = {'BASH_ENV': hook, LOG_DIR: str(log_dir)}
    own = env.get('BASH_ENV')
    if own and any(char in own for char in '$`'):
        ghio.warning(f'input shell replaces BASH_ENV {own}: bash expands it, so the bash scripts'
                     ' the tests start no longer read it')
    elif own:
        added[OWN_BASH_ENV] = own if '/' in own else './' + own
    ghio.notice('shell coverage measures bash scripts that inherit the environment; a script'
                ' started with a cleared environment (env -i) is not measured')
    return added


def _holds(scan: Scan, line: int, command: str) -> bool:
    """Whether the line holds `command`, printed verbatim up to a `$(`, backquote or `(`."""
    for text in scan.words.get(line, ()):
        cut = min((text.find(mark) for mark in ('$(', '`', '(') if mark in text), default=-1)
        if (command.startswith(text[:max(cut, 1)]) if cut >= 0 else
                command == text or command.startswith(text + ' ')):
            return True
    return False


def _credited(scan: Scan, line: int, fields: List[str], frozen: bool) -> Optional[int]:
    """The line a record provably ran on, or None. In a trap handler BASH_COMMAND stays the
    trapped command (`frozen`), so there a function of the file's own counts unchecked."""
    first, command, starter, function = fields
    if function not in ('', 'source') and function not in scan.functions:
        return None
    if first == 'c':
        return line
    if first.isdecimal():
        if int(first) in scan.forks and _holds(scan, int(first), starter):
            pass
        elif int(first) in scan.bases:
            return int(first)
        else:
            return None
    past = scan.stop is not None and line >= scan.stop or frozen and function in scan.functions
    return line if past or line in scan.entries or _holds(scan, line, command) else None


def collect(repo: Union[str, Path], patterns_text: str, log_dir: Union[str, Path]) -> Lines:
    """Line hits of every tracked script the `shell` globs match; {} when nothing was armed."""
    if not os.path.isdir(log_dir):
        return {}
    files = split.match(split.parse_patterns(patterns_text, 'shell'), split.tracked_files(repo),
                        'shell')
    scans: Dict[str, Tuple[Scan, int]] = {}
    lines: Lines = {}
    for name in files:
        text = Path(repo, name).read_bytes().decode('utf-8', 'replace')
        scan = Scan(text)
        scans[name] = scan, text.count('\n') + (not text.endswith('\n'))
        lines[name] = dict.fromkeys(sorted(scan.lines), 0)
        if scan.stop is not None:
            ghio.warning(f'shell coverage cannot read {name} past line {scan.stop}: from there'
                         ' on only the lines bash ran are counted')
    names: Dict[str, Optional[str]] = {}
    for log in sorted(Path(log_dir).iterdir()):
        previous = None
        for record in log.read_bytes().decode('utf-8', 'surrogateescape').split('\n'):
            source, number, *fields = (record.rsplit('\t', 5) + [''] * 5)[:6]
            frozen, previous = fields[1] == previous, fields[1]
            if source not in names:
                names[source] = source and paths.repo_relative(os.path.realpath(source), str(repo))
            name = names[source] or ''
            if name in lines and number.isdecimal():
                scan, count = scans[name]
                line = _credited(scan, int(number), fields, frozen)
                if line is not None and (line in lines[name] or
                                         scan.stop is not None and scan.stop <= line <= count):
                    lines[name][line] = lines[name].get(line, 0) + 1
    return lines
