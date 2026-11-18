"""Run a composite action's steps the way the runner does, stdlib only.

The YAML reader and the runner model only the shapes this repository's action files use and
raise AssertionError on anything else; `run_action` lists what a caller must state.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
KEY = re.compile(r'([A-Za-z0-9_-]+):(?: +(.*))?')
BLOCK = re.compile(r'(\|-?)(?:\s+#.*)?')
SINGLE = re.compile(r"'((?:[^']|'')*)'(?:\s+#.*)?")
DOUBLE = re.compile(r'"([^"\\]*)"(?:\s+#.*)?')
RUN_KEYS = {'id', 'name', 'shell', 'env', 'run', 'if'}
USES_KEYS = {'id', 'name', 'uses', 'with', 'if'}


def _refuse(what: str) -> 'AssertionError':
    return AssertionError(f'unmodelled YAML: {what}')


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(' '))


def _value(text: str) -> str:
    if text[0] == "'":
        found = SINGLE.fullmatch(text)
        if not found:
            raise _refuse(f'single-quoted value {text!r}')
        return found.group(1).replace("''", "'")
    if text[0] == '"':
        found = DOUBLE.fullmatch(text)
        if not found:
            raise _refuse(f'double-quoted value {text!r} (escapes are not modelled)')
        return found.group(1)
    if text[0] in '[{&*!>|%@`#?' or text.startswith('- '):
        raise _refuse(f'value {text!r}')
    plain = re.split(r'\s+#', text, maxsplit=1)[0].rstrip()
    if ': ' in plain or plain.endswith(':'):
        raise _refuse(f'plain value {text!r} holds a mapping indicator')
    return plain


def parse(text: str) -> Any:
    if '\r' in text:
        raise _refuse('a carriage return')
    lines = text.split('\n')

    def skip(index: int) -> int:
        while index < len(lines) and (not lines[index].strip()
                                      or lines[index].lstrip().startswith('#')):
            index += 1
        return index

    def block(index: int, base: int, keep: bool) -> Tuple[str, int]:
        body = []
        while index < len(lines) and (not lines[index].strip() or _indent(lines[index]) > base):
            body.append(lines[index])
            index += 1
        content = [line for line in body if line.strip()]
        if not content:
            raise _refuse(f'an empty block scalar before line {index + 1}')
        width = _indent(content[0])
        if any(_indent(line) < width for line in content):
            raise _refuse(f'a block scalar line dedented below its first, before line {index + 1}')
        joined = '\n'.join(line[width:] for line in body).rstrip('\n')
        return joined + ('\n' if keep else ''), index

    def node(index: int, indent: int) -> Tuple[Any, int]:
        index = skip(index)
        if index >= len(lines) or _indent(lines[index]) != indent:
            raise _refuse(f'expected content indented {indent} at line {index + 1}')
        if lines[index].lstrip().startswith('-'):
            return sequence(index, indent)
        return mapping(index, indent)

    def sequence(index: int, indent: int) -> Tuple[Any, int]:
        items = []
        while index < len(lines) and _indent(lines[index]) == indent \
                and lines[index].lstrip().startswith('-'):
            found = re.fullmatch(r'- (\S.*)', lines[index].strip())
            if not found:
                raise _refuse(f'list item {lines[index].strip()!r} is not "- key: value"')
            lines[index] = ' ' * (indent + 2) + found.group(1)
            item, index = mapping(index, indent + 2)
            items.append(item)
            index = skip(index)
        return items, index

    def mapping(index: int, indent: int) -> Tuple[Any, int]:
        result: Dict[str, Any] = {}
        while index < len(lines) and _indent(lines[index]) == indent \
                and not lines[index].lstrip().startswith('-'):
            if '\t' in lines[index][:len(lines[index]) - len(lines[index].lstrip())]:
                raise _refuse(f'a tab in the indentation of line {index + 1}')
            found = KEY.fullmatch(lines[index].strip())
            if not found:
                raise _refuse(f'line {index + 1} {lines[index].strip()!r} is not "key: value"')
            key, value = found.group(1), (found.group(2) or '').strip()
            if key in result:
                raise _refuse(f'key {key} repeats at line {index + 1}')
            index += 1
            marker = BLOCK.fullmatch(value) if value[:1] == '|' else None
            if marker:
                result[key], index = block(index, indent, marker.group(1) == '|')
            elif value:
                result[key] = _value(value)
            else:
                following = skip(index)
                if following >= len(lines) or _indent(lines[following]) <= indent:
                    raise _refuse(f'key {key} at line {index} has no value')
                result[key], index = node(following, _indent(lines[following]))
            index = skip(index)
        return result, index

    start = skip(0)
    if start >= len(lines):
        raise _refuse('an empty document')
    parsed, end = node(start, 0)
    end = skip(end)
    if end < len(lines):
        raise _refuse(f'line {end + 1} {lines[end].strip()!r} was not consumed')
    return parsed


class Outcome(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    outputs: Dict[str, str]
    ran: List[str]


Handler = Callable[[Dict[str, str]], None]


def SKIP(_with_values: Dict[str, str]) -> None:
    """The `uses` treatment for a step that does nothing here."""


def _render(value: str, inputs: Dict[str, str]) -> str:
    if '${{' not in value:
        return value
    whole = re.fullmatch(r'\$\{\{ inputs\.([A-Za-z0-9_-]+) \}\}', value)
    if not whole:
        raise AssertionError(f'unmodelled expression {value!r}')
    if whole.group(1) not in inputs:
        raise AssertionError(f'expression {value!r} names an undeclared input')
    return inputs[whole.group(1)]


def action_spec(action: str, root: Path = ROOT) -> Dict[str, Any]:
    spec = parse((root / action / 'action.yml').read_text(encoding='utf-8'))
    runs = spec.get('runs')
    if not isinstance(runs, dict) or runs.get('using') != 'composite' \
            or not isinstance(runs.get('steps'), list) or not runs['steps']:
        raise AssertionError(f'{action}/action.yml is not a composite action with steps')
    return spec


def _checked(step: Dict[str, Any]) -> str:
    """The step's kind, after refusing keys and shells this harness does not model."""
    kind = 'run' if 'run' in step else 'uses' if 'uses' in step else ''
    allowed = {'run': RUN_KEYS, 'uses': USES_KEYS}.get(kind)
    if not allowed or ('run' in step and 'uses' in step):
        raise AssertionError(f'step {step!r} is neither a run: nor a uses: step')
    extra = sorted(set(step) - allowed)
    if extra:
        raise AssertionError(f'step {step.get("name", step)!r}: keys {extra} are not modelled')
    if kind == 'run' and step.get('shell') != 'bash':
        raise AssertionError(f'step {step.get("name", step)!r}: shell must be bash,'
                             f' got {step.get("shell")!r}')
    for table in ('env', 'with'):
        if table in step and not isinstance(step[table], dict):
            raise AssertionError(f'step {step.get("name", step)!r}: {table} is not a mapping')
    return kind


def _written(path: Path) -> Dict[str, str]:
    written = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        key, separator, value = line.partition('=')
        if not separator:
            raise AssertionError(f'unmodelled GITHUB_OUTPUT line {line!r}')
        written[key] = value
    return written


def _resolve_outputs(declared: Dict[str, Any], ids: List[str], step_outputs: Dict[str, Path],
                     returncode: int) -> Dict[str, str]:
    """The action's declared outputs, each from exactly `${{ steps.<id>.outputs.<name> }}`.

    After a failing run an output its step never wrote is left out; otherwise it is refused."""
    resolved = {}
    for name, item in declared.items():
        value = item.get('value', '') if isinstance(item, dict) else ''
        found = re.fullmatch(r'\$\{\{ steps\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+) \}\}',
                             value)
        if not found:
            raise AssertionError(f'output {name}: unmodelled value {value!r}')
        step_id, key = found.groups()
        if step_id not in ids:
            raise AssertionError(f'output {name}: the action has no step with id {step_id}')
        written = _written(step_outputs[step_id]) if step_id in step_outputs else {}
        if key in written:
            resolved[name] = written[key]
        elif not returncode:
            raise AssertionError(f'output {name}: step {step_id} did not write {key}')
    for step_id, path in step_outputs.items():
        _written(path)
    return resolved


def run_action(action: str, inputs: Dict[str, str], cwd: Path, interpreter: Optional[str] = None,
               *, uses: Optional[Mapping[str, Handler]] = None,
               conditions: Optional[Mapping[str, bool]] = None, root: Path = ROOT) -> Outcome:
    """Execute `<action>/action.yml` from `cwd`: `run:` steps with bash, `uses:` steps through
    the caller's handler.

    Inputs not given take the action's declared default; one the action does not declare is
    refused. `uses` maps an action name (the part before `@`) to a handler called with the
    rendered `with:` values (`SKIP` to do nothing); `conditions` maps an `if:` expression's exact
    text to whether it holds. A step using one the caller did not list is refused. A step that
    fails ends the run, and a later step carrying `if:` is refused (always() and failure() are
    not modelled). `interpreter` names the only Python on PATH (`python3` or `python`) so the
    action's interpreter resolution is exercised. `Outcome.outputs` holds only the declared
    `outputs:`, each resolved from its step's own GITHUB_OUTPUT."""
    spec = action_spec(action, root)
    declared = spec.get('inputs', {})
    undeclared = sorted(set(inputs) - set(declared))
    if undeclared:
        raise AssertionError(f'{action} declares no input {undeclared}')
    values = {name: item.get('default', '') for name, item in declared.items()}
    values.update(inputs)
    steps = spec['runs']['steps']
    kinds = [_checked(step) for step in steps]
    ids = [step['id'] for step in steps if 'id' in step]
    if len(set(ids)) != len(ids):
        raise AssertionError(f'{action} repeats a step id: {ids}')
    with tempfile.TemporaryDirectory() as scratch:
        step_outputs: Dict[str, Path] = {}
        path = os.environ['PATH']
        if interpreter:
            bin_dir = Path(scratch, 'bin')
            bin_dir.mkdir()
            (bin_dir / interpreter).symlink_to(sys.executable)
            (bin_dir / 'git').symlink_to(str(shutil.which('git')))
            path = str(bin_dir)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('TEST_RATCHET_', 'GITHUB_'))}
        env.update(PATH=path, GITHUB_ACTION_PATH=str(root / action),
                   GITHUB_WORKSPACE=str(cwd))
        stdout, stderr, returncode, ran = '', '', 0, []
        for number, (step, kind) in enumerate(zip(steps, kinds), 1):
            name = step.get('name') or f'step {number}'
            if 'if' in step:
                if returncode:
                    raise AssertionError(f'step {name!r} has an if: after a failing step:'
                                         ' always() and failure() are not modelled')
                expression = step['if']
                if conditions is None or expression not in conditions:
                    raise AssertionError(f'step {name!r}: no treatment stated for if:'
                                         f' {expression!r}')
                if not conditions[expression]:
                    continue
            elif returncode:
                break
            ran.append(name)
            if kind == 'uses':
                handler = (uses or {}).get(step['uses'].split('@')[0])
                if handler is None:
                    raise AssertionError(f'step {name!r}: no treatment stated for uses:'
                                         f' {step["uses"]!r}')
                handler({key: _render(value, values)
                         for key, value in step.get('with', {}).items()})
                continue
            github_output = Path(scratch, f'output-{number}')
            github_output.write_text('', encoding='utf-8')
            if 'id' in step:
                step_outputs[step['id']] = github_output
            step_env = dict(env, GITHUB_OUTPUT=str(github_output))
            step_env.update({key: _render(value, values)
                             for key, value in step.get('env', {}).items()})
            done = subprocess.run([str(shutil.which('bash')), '--noprofile', '--norc', '-eo',
                                   'pipefail', '-c', step['run']], cwd=cwd, env=step_env,
                                  capture_output=True, text=True, check=False, timeout=120)
            stdout += done.stdout
            stderr += done.stderr
            returncode = done.returncode
        outputs = _resolve_outputs(spec.get('outputs', {}), ids, step_outputs, returncode)
    return Outcome(returncode, stdout, stderr, outputs, ran)
