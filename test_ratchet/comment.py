"""The pull request comment: one per pull request, found by its marker and edited in place."""
import json
import os
import re
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from test_ratchet import ConfigError, cobertura, ghio, github, report
from test_ratchet.report import code

MARKER = '<!-- test-ratchet-report -->'
BOT = 'github-actions[bot]'
LIMIT = 65536
HUNK = re.compile(r'@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')
# Per language, per file with a measured added line: its measured added lines, how many are
# covered and the uncovered ones.
Measured = Dict[str, Dict[str, Tuple[int, int, List[int]]]]


class Outcome(NamedTuple):
    """What the check run input comment-check names concludes, its title and its summary."""
    conclusion: str
    title: str
    summary: str


class Unresolved(ConfigError):
    """The pull request lookup failed: on workflow_run a fork's data can cause it. `outcome` is
    the check run's, None when no pull request is the comment's destination."""

    def __init__(self, text: str, outcome: Optional[Outcome]) -> None:
        super().__init__(text)
        self.outcome = outcome


class Target(NamedTuple):
    client: github.Client
    repo: str
    number: int
    head: str
    base: str


def _event(name: str) -> Dict[str, Any]:
    path = os.environ.get('GITHUB_EVENT_PATH', '')
    try:
        with open(path, encoding='utf-8') as handle:
            event = json.load(handle)
    except (OSError, ValueError) as error:
        raise ConfigError(f'GITHUB_EVENT_PATH {path!r} names no {name} payload: {error}') \
            from None
    if not isinstance(event, dict):
        raise ConfigError(f'GITHUB_EVENT_PATH {path!r} is not a {name} payload')
    return event


def gating(enabled: bool) -> bool:
    """Whether input workflow-run-gate makes this run, a `workflow_run` run of a pull request's
    run, a gate."""
    if not enabled or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_run':
        return False
    return (_event('workflow_run').get('workflow_run') or {}).get('event') == 'pull_request'


def target(token: str, gate: bool = False) -> Tuple[Optional[Target], Optional[Outcome]]:
    """The pull request a `pull_request` or `workflow_run` run belongs to, from the event and the
    API, never from an artifact; None when there is none or it has moved past the run, which a
    gating run refuses instead; and the check run's failure when several hold the run's head."""
    name = os.environ.get('GITHUB_EVENT_NAME', '')
    if name not in ('pull_request', 'workflow_run'):
        return None, None
    event = _event(name)
    if name == 'workflow_run' and (event.get('workflow_run') or {}).get('event') != 'pull_request':
        return None, None
    if not token:
        raise ConfigError('input github-token is empty: the pull request comment needs it')
    try:
        return _resolve(github.Client(token), os.environ.get('GITHUB_REPOSITORY', ''), name,
                        event, gate)
    except Unresolved:
        raise
    except ConfigError as error:
        raise Unresolved(str(error), Outcome(
            'failure', 'the pull request lookup failed',
            f'The pull request lookup failed, so no comment was written: {code(str(error))}')) \
            from None


def _skip(gate: bool, reason: str, many: bool) -> Tuple[None, Optional[Outcome]]:
    outcome = Outcome('failure', reason, f'No comment was written: {code(reason)}') if many \
        else None
    if gate:
        raise Unresolved(reason, outcome)
    ghio.notice(f'no pull request comment: {reason}')
    return None, outcome


def _resolve(client: github.Client, repo: str, name: str, event: Dict[str, Any],
             gate: bool) -> Tuple[Optional[Target], Optional[Outcome]]:
    gone = False
    try:
        if name == 'pull_request':
            head, numbers = event['pull_request']['head']['sha'], {event['pull_request']['number']}
        else:
            run = event['workflow_run']
            head = run['head_sha']
            numbers = {item['number'] for item in run['pull_requests']}
            if not numbers:
                # A fork's pull request is not in the event: the head repository names it, and
                # a deleted fork leaves none, so no pull request is the destination.
                gone = run['head_repository'] is None
                held = client.pages(f'repos/{run["head_repository"]["full_name"]}/commits/{head}'
                                    '/pulls?per_page=100')
                numbers = {item['number'] for item in held
                           if item['base']['repo']['full_name'] == repo}
    except (KeyError, TypeError) as error:
        text = f'GITHUB_EVENT_PATH: the {name} payload has no {error}'
        if gone:
            raise Unresolved(text, None) from None
        raise ConfigError(text) from None
    if len(numbers) != 1:
        return _skip(gate, f'{len(numbers)} pull requests of {repo} hold {head}', len(numbers) > 1)
    number = numbers.pop()
    if not isinstance(number, int):
        raise ConfigError(f'GITHUB_EVENT_PATH: pull request number {number!r} is no integer')
    current = client.call('GET', f'repos/{repo}/pulls/{number}')
    try:
        now, base = current['head']['sha'], current['base']['sha']
    except (KeyError, TypeError):
        raise ConfigError(f'GitHub API: pull request #{number} has no head and base SHA') \
            from None
    if now != head:
        return _skip(gate, f"#{number} is at {now}, not at the run's head {head}", False)
    return Target(client, repo, number, head, base), None


def added(patch: str) -> List[int]:
    """The new-file line numbers of a patch's added lines, counted from its hunk headers."""
    numbers, line = [], 0
    for text in patch.split('\n'):
        found = HUNK.match(text)
        if found:
            line = int(found.group(1))
        elif text.startswith('+'):
            numbers.append(line)
            line += 1
        elif not text.startswith(('-', '\\')):
            line += 1
    return numbers


def diff_coverage(files: List[Any], coverage: Dict[str, cobertura.Lines]) \
        -> Tuple[Measured, List[str]]:
    """Per language, the added lines its coverage measures; and the files that add lines
    GitHub sent no patch for."""
    patches, unmeasured = {}, []
    for item in files:
        name, patch = item.get('filename'), item.get('patch')
        if not isinstance(name, str) or not isinstance(patch, (str, type(None))):
            raise ConfigError(f'GitHub API: a pull request file is not a filename and patch:'
                              f' {item!r:.200}')
        if patch is not None:
            patches[name] = added(patch)
        elif item.get('additions') != 0:
            unmeasured.append(name)
    measured: Measured = {}
    for language, lines in coverage.items():
        measured[language] = {}
        for name, numbers in patches.items():
            hits = lines.get(name, {})
            known = [number for number in numbers if number in hits]
            uncovered = [number for number in known if not hits[number]]
            if known:
                measured[language][name] = (len(known), len(known) - len(uncovered), uncovered)
    return measured, sorted(unmeasured)


def ranges(numbers: List[int]) -> str:
    spans: List[List[int]] = []
    for number in sorted(numbers):
        if spans and number == spans[-1][1] + 1:
            spans[-1][1] = number
        else:
            spans.append([number, number])
    return ', '.join(str(first) if first == last else f'{first}-{last}' for first, last in spans)


def _sums(files: Dict[str, Tuple[int, int, List[int]]]) -> Tuple[int, int, int]:
    """Added, covered and missed lines summed over `files`."""
    total = sum(count for count, _, _ in files.values())
    covered = sum(covered for _, covered, _ in files.values())
    return total, covered, total - covered


def _files(count: int, other: str = '') -> str:
    return f'{count} {other}file' + 's' * (count != 1)


def render(result: report.Report, head: str, measured: Measured,
           unmeasured: List[str]) -> Tuple[str, str]:
    """The comment's totals, then its details."""
    text = [MARKER, f'### test-ratchet coverage, cell {code(result.cell)}', '', f'Commit {head}.',
            '']
    if result.left_out:
        text += ['Left out of the merge, so the coverage below is measured without them:', '']
        text += [f'- {code(item)}' for item in result.left_out] + ['']
    if result.totals or result.unmeasured:
        text += ['| Language | Lines covered | Coverage | Floor | Result | Added | Covered |'
                 ' Missed |', '|---|---|---|---|---|---|---|---|']
        text += [report.row(total) + ' {} | {} | {} |'.format(*_sums(measured[total.language]))
                 for total in result.totals]
        text += [report.unmeasured_row(*item) + ' none | none | none |'
                 for item in result.unmeasured.items()]
        text += ['', f'Floors from the {result.source}.'] * bool(result.source)
    else:
        text.append('No coverage was merged.')
    rows, heading = [], []
    for language, files in measured.items():
        missed = {name: item for name, item in files.items() if item[2]}
        if not missed:
            continue
        total, _, uncovered = _sums(files)
        heading.append(f'{language} {uncovered} of {total}, in {len(missed)} of'
                       f' {_files(len(files))}')
        # An escaped pipe keeps a file name inside its cell, even within the code span.
        rows += ['| {} | {} | {} | {} | {} | {} |'.format(
            language, code(name).replace('|', '\\|'), count, covered, count - covered,
            ranges(numbers)) for name, (count, covered, numbers) in sorted(missed.items())]
        rest = {name: item for name, item in files.items() if name not in missed}
        if rest:
            rows.append('| {} | {}, fully covered | {} | {} | {} | |'.format(
                language, _files(len(rest), 'other '), *_sums(rest)))
    sections = []
    if rows:
        sections.append(f'Uncovered added lines: {"; ".join(heading)}\n\n| Language | File |'
                        ' Added | Covered | Missed | Missed lines |\n|---|---|---|---|---|---|\n'
                        + '\n'.join(rows))
    if unmeasured:
        sections.append('Not measured, GitHub sent no patch: ' + ', '.join(map(code, unmeasured)))
    return '\n'.join(text) + '\n', ''.join(f'\n{section}\n' for section in sections)


def publish(where: Target, result: report.Report) -> Outcome:
    """Write the comment, or edit the one already there; a token that may not write puts the
    text in the job summary instead."""
    files = where.client.pages(f'repos/{where.repo}/pulls/{where.number}/files?per_page=100')
    totals, details = render(result, where.head, *diff_coverage(files, result.coverage))
    body = full = totals + details
    if len(full) > LIMIT:
        body = totals + ('\nThe rest is too long for a comment: it is in the job summary of'
                         f' {os.environ.get("GITHUB_SERVER_URL", "https://github.com")}/'
                         f'{where.repo}/actions/runs/{os.environ.get("GITHUB_RUN_ID", "")}.\n')
    return _write(where, body, full, 'the coverage report')


def refusal(where: Target, text: str) -> Outcome:
    """The comment of a run whose artifacts were refused."""
    body = f'{MARKER}\n### test-ratchet report refused the artifacts\n\n{code(text)}\n'
    return _write(where, body, body, f'the refusal of the artifacts: {code(text)}')


def unwritten(where: Target, error: ConfigError) -> Outcome:
    return Outcome('failure', 'the comment could not be written',
                   f'The comment on pull request #{where.number} could not be written:'
                   f' {code(str(error))}')


def _write(where: Target, body: str, full: str, holds: str) -> Outcome:
    try:
        comments = where.client.pages(f'repos/{where.repo}/issues/{where.number}/comments'
                                      '?per_page=100')
        mine = [item['id'] for item in comments if (item.get('user') or {}).get('login') == BOT
                and str(item.get('body') or '').startswith(MARKER)]
        if mine:
            where.client.call('PATCH', f'repos/{where.repo}/issues/comments/{int(mine[0])}',
                              {'body': body})
        else:
            where.client.call('POST', f'repos/{where.repo}/issues/{where.number}/comments',
                              {'body': body})
    except github.ApiError as error:
        if error.status not in (403, 404):
            raise
        ghio.notice(f'the pull request comment could not be written, its text is in the job'
                    f' summary: {error}')
        ghio.add_summary(full)
        return unwritten(where, error)
    if body is not full:
        ghio.add_summary(full)
    done = f'{"updated the comment" if mine else "commented"} on pull request #{where.number}'
    print(f'test-ratchet report: {done}')
    return Outcome('success', done, f'The comment on pull request #{where.number} holds {holds}.')


def check(token: str, name: str, outcome: Outcome) -> None:
    """Publish check run `name`, completed with `outcome`, on the head of the triggering run."""
    head = (_event('workflow_run').get('workflow_run') or {}).get('head_sha')
    if not (isinstance(head, str) and head):
        ghio.warning(f'no check run {name}: the workflow_run payload has no head_sha')
        return
    try:
        github.Client(token).call('POST', f'repos/{os.environ.get("GITHUB_REPOSITORY", "")}'
                                  '/check-runs', {
                                      'name': name, 'head_sha': head, 'status': 'completed',
                                      'conclusion': outcome.conclusion,
                                      'output': {'title': outcome.title,
                                                 'summary': outcome.summary}})
    except github.ApiError as error:
        if error.status not in (403, 404):
            raise
        raise ConfigError(f'check run {name} needs checks: write in the workflow\'s'
                          f' permissions: {error}') from None
