"""V8 coverage JSON (NODE_V8_COVERAGE files, browser dumps) as tracked-file line hits."""
import bisect
import json
import re
from pathlib import Path
from typing import Collection, Dict, Iterable, List, Optional, Set, Tuple, TypeGuard
from urllib.parse import unquote, urlsplit

from test_ratchet import ConfigError, ghio, jslines, paths, split
from test_ratchet.cobertura import Lines, merge
from test_ratchet.xmlio import Source

Range = Tuple[int, int, int]


def file_path(url: str) -> str:
    path = unquote(urlsplit(url).path)
    return path[1:] if re.match(r'/[A-Za-z]:/', path) else path


def _integer(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _entry(dump: Source, index: int, entry: object) -> Tuple[str, Optional[str], List[Range]]:
    where = f'V8 coverage dump {dump}: entry {index}'
    if not isinstance(entry, dict) or not isinstance(entry.get('url'), str):
        raise ConfigError(f'{where} is not an object with a string url')
    try:
        urlsplit(entry['url'])
    except ValueError as error:
        raise ConfigError(f'{where} has a url that cannot be parsed: {error}') from error
    raw = entry.get('rawScriptCoverage')
    functions, source = (raw.get('functions'), entry.get('text')) if isinstance(raw, dict) \
        else (entry.get('functions'), entry.get('source'))
    if source is not None and not isinstance(source, str):
        raise ConfigError(f'{where} has a source that is not a string')
    if not isinstance(functions, list):
        hint = ' (Puppeteer writes them with includeRawScriptCoverage)' if 'text' in entry else ''
        raise ConfigError(f'{where} has no functions list{hint}')
    ranges = []
    for function in functions:
        found = function.get('ranges') if isinstance(function, dict) else None
        if not isinstance(found, list):
            raise ConfigError(f'{where} has a function without a ranges list')
        for item in found:
            start, end, count = (item.get(key) if isinstance(item, dict) else None
                                 for key in ('startOffset', 'endOffset', 'count'))
            if not (_integer(start) and _integer(end) and _integer(count) and start <= end):
                raise ConfigError(f'{where} has an invalid range {item!r}')
            ranges.append((start, end, count))
    return entry['url'], source, ranges


class _Torn(ConfigError):
    """Read, but not JSON text."""


def _entries(dump: Source) -> list:
    try:
        data = json.loads(Path(dump).read_text(encoding='utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise _Torn(f'V8 coverage dump {dump} cannot be read as JSON: {error}') from error
    except (OSError, ValueError, RecursionError) as error:
        raise ConfigError(f'V8 coverage dump {dump} cannot be read as JSON: {error}') from error
    entries = data.get('result') if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ConfigError(f'V8 coverage dump {dump} is neither a list of script entries nor'
                          ' an object whose result is one')
    return entries


def _units(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def _hits(text: str, first_line: int, ranges: List[Range], any_character: bool = False
          ) -> Dict[int, int]:
    """A line with a non-blank character counts the innermost range holding the first, or with
    `any_character` the most hits of the innermost ranges at its characters;
    offsets are UTF-16 units."""
    # The innermost range changes only at a range's ends.
    ends = sorted({end for start, stop, _ in ranges for end in (start, stop)}) \
        if any_character else []
    points = []
    offset = 0
    for number, line in enumerate(text.split('\n'), first_line):
        body = re.sub(r'^[\s\ufeff]+', '', line)  # JavaScript's whitespace holds U+FEFF
        stop = offset + _units(line)
        if body and any_character:
            points.append((offset, number))
            points.extend((end, number) for end in
                          ends[bisect.bisect_right(ends, offset):bisect.bisect_left(ends, stop)])
        elif body:
            points.append((offset + _units(line[:len(line) - len(body)]), number))
        offset = stop + 1
    # Ranges nest, so a stack ordered by start (outer first on equal starts) holds every
    # range open at a point, innermost on top; the stable sort lets a later equal range win.
    pending = sorted(ranges, key=lambda item: (item[0], -item[1]))
    stack: List[Range] = []
    hits: Dict[int, int] = {}
    taken = 0
    for point, number in points:
        while taken < len(pending) and pending[taken][0] <= point:
            stack.append(pending[taken])
            taken += 1
        while stack and stack[-1][1] <= point:
            stack.pop()
        if stack:
            hits[number] = max(hits.get(number, 0), stack[-1][2])
    return hits


def _masked(text: str, data: re.Pattern[str]) -> str:
    # shortcut: a line holding only NUL reads as masked, upgrade if a script ever holds one
    return '\n'.join('\0' if data.fullmatch(line) else line for line in text.split('\n'))


class _Converter:
    def __init__(self, repo: Source, code_only: bool, kept: Optional[Set[str]],
                 data: Optional[re.Pattern[str]], any_character: bool):
        self.root = str(repo)
        self.code_only = code_only
        self.any_character = any_character
        self.data = data
        self.kept = kept
        self.code: Dict[Tuple[str, int, str], Optional[Set[int]]] = {}
        self.refused: Dict[str, None] = {}  # an ordered set of messages
        self.tracked: Set[str] = set(split.tracked_files(repo))
        self.index = paths.Index(self.tracked)
        self.texts: Dict[str, Optional[str]] = {}
        self.unmatched: Set[str] = set()
        self.ambiguous: Dict[str, List[str]] = {}
        self.unplaced: Set[str] = set()

    def locate(self, url: str) -> Optional[str]:
        parts = urlsplit(url)
        if parts.scheme == 'file':
            name = paths.repo_relative(file_path(url), self.root)
            return name if name in self.tracked else None
        if parts.scheme not in ('http', 'https'):
            return None
        segments = [part for part in unquote(parts.path).split('/') if part]
        found, tied = self.index.match(segments)
        if tied:
            self.ambiguous[url] = tied
        elif not found:
            self.unmatched.add(url)
        return found

    def text(self, name: str) -> Optional[str]:
        if name not in self.texts:
            try:
                self.texts[name] = Path(self.root, name).read_bytes().decode('utf-8')
            except (OSError, UnicodeDecodeError):
                self.texts[name] = None
        return self.texts[name]

    def lines(self, url: str, source: Optional[str], ranges: List[Range]) -> Lines:
        name = self.locate(url)
        if name is None or not ranges:
            return {}
        text = self.text(name)
        if text is not None and source is not None and source != text:
            # Served text found once in the file (an inline <script>) keeps its line numbers,
            # and so does one found once when both sides' data lines are masked.
            found, served = (text, source) if text.count(source) == 1 or self.data is None \
                else (_masked(text, self.data), _masked(source, self.data))
            start = found.find(served) if found.count(served) == 1 else -1
            if start < 0:
                text = None
            else:
                return self.hits(name, source, 1 + found.count('\n', 0, start), ranges)
        end = max((end for _, end, _ in ranges), default=-1)
        if text is not None and text.startswith('\ufeff') and end == _units(text) - 1:
            text = text[1:]  # Node strips an ES module's byte order mark, not a CommonJS one's.
        if text is not None and end != _units(text):
            # Without a source the script's range must span the file, or it ran other code.
            text = None
        if text is None:
            self.unplaced.add(f'{url} ({name})')
            return {}
        return self.hits(name, text, 1, ranges)

    def hits(self, name: str, text: str, first_line: int, ranges: List[Range]) -> Lines:
        if self.kept is not None and name not in self.kept:
            return {}  # placed and logged as before, but neither measured nor scanned
        found = _hits(text, first_line, ranges, self.any_character)
        if not self.code_only:
            return {name: found}
        key = (name, first_line, text)
        if key not in self.code:
            try:
                self.code[key] = jslines.code_lines(text, name, first_line)
            except ConfigError as error:
                self.code[key] = None
                self.refused[f'input javascript-code-only cannot tell code from comments in'
                             f' {error}'] = None
        code = self.code[key]
        return {} if code is None else {
            name: {line: hits for line, hits in found.items() if line in code}}

    def log(self) -> None:
        if self.unmatched:
            ghio.notice('V8 coverage skipped for URLs matching no tracked file: '
                        + ', '.join(sorted(self.unmatched)))
        if self.ambiguous:
            ghio.notice('V8 coverage skipped for URLs matching several tracked files equally: '
                        + ', '.join(f'{url} ({" ".join(names)})'
                                    for url, names in sorted(self.ambiguous.items())))
        if self.unplaced:
            ghio.notice('V8 coverage skipped where the file is not UTF-8, the script source is'
                        ' not found once in it, or the offsets do not span it: '
                        + ', '.join(sorted(self.unplaced)))


def convert(dumps: Iterable[Source], repo: Source, unread: Optional[List[str]] = None,
            code_only: bool = False, kept: Optional[Set[str]] = None,
            data: Optional[re.Pattern[str]] = None, any_character: bool = False,
            node: Collection[Source] = ()) -> Lines:
    """The dumps' line hits of the tracked files in `kept` (all when None), only of lines holding
    code when `code_only`, a served source also placed where it differs from the file only in
    whole lines `data` matches, a line counting any of its characters when `any_character`; a
    dump that cannot be read, or a kept file whose code lines cannot be told, is named in `unread`
    when given. A dump in `node` that is empty or cut short is left out and named in a notice."""
    converter = _Converter(repo, code_only, kept, data, any_character)
    found = []
    node, torn = set(node), []
    for dump in dumps:
        try:
            entries = [_entry(dump, index, entry) for index, entry in enumerate(_entries(dump))]
        except ConfigError as error:
            if isinstance(error, _Torn) and dump in node:
                torn.append(str(dump))
                continue
            if unread is None:
                raise
            unread.append(str(error))
            continue
        found.extend(converter.lines(*entry) for entry in entries)
    converter.log()
    if torn:
        ghio.notice('V8 coverage skipped for NODE_V8_COVERAGE dumps that are empty or cut short: '
                    + ', '.join(torn))
    if converter.refused:
        if unread is None:
            raise ConfigError(next(iter(converter.refused)))
        unread.extend(converter.refused)
    return merge(found)


def unloaded(repo: Source, names: Iterable[str], code_only: bool, unread: List[str]) -> Lines:
    """The script files `names` at 0 hits on each line holding a non-blank character, or code
    when `code_only`; one unreadable as UTF-8 or as code is named in `unread`."""
    found: Lines = {}
    others = []
    for name in sorted(names):
        if not name.endswith(('.js', '.mjs', '.cjs')):
            others.append(name)
            continue
        try:
            text = Path(repo, name).read_bytes().decode('utf-8-sig')
            lines = jslines.code_lines(text, name) if code_only else {
                number for number, line in enumerate(text.split('\n'), 1)
                if re.search(r'[^\s\ufeff]', line)}
        except (OSError, UnicodeDecodeError) as error:
            unread.append(f'input javascript-all-files cannot read {name}: {error}')
            continue
        except ConfigError as error:
            unread.append(f'input javascript-code-only cannot tell code from comments in {error}')
            continue
        if lines:
            found[name] = dict.fromkeys(lines, 0)
    if others:
        ghio.notice('input javascript-all-files adds only .js, .mjs and .cjs files, so not: '
                    + ', '.join(others))
    return found
