"""JUnit reports: each <testcase> given its repository-relative `file`, seconds per file."""
import math
import posixpath
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

from test_ratchet import ConfigError, ghio, paths, times
from test_ratchet.xmlio import Source, read_xml


class _Files:

    def __init__(self, files: Sequence[str]):
        self.index = paths.Index(files)
        self.by_module: Dict[str, List[Tuple[List[str], str]]] = defaultdict(list)
        for path in files:
            module = posixpath.splitext(path)[0].split('/')
            self.by_module[module[-1]].append((module, path))

    def as_path(self, text: str) -> Optional[str]:
        segments = [part for part in times.normalise(text).split('/') if part]
        return self.index.match(segments)[0]

    def as_module(self, text: str) -> Optional[str]:
        """The file whose module ends with a leading part of pytest's dotted classname."""
        segments = text.split('.')
        scored = []
        for length in range(1, len(segments) + 1):
            for module, path in self.by_module.get(segments[length - 1], ()):
                if module[-length:] == segments[:length]:
                    scored.append(((length, length == len(module)), path))
        best = max((score for score, _ in scored), default=None)
        winners = sorted({path for score, path in scored if score == best})
        return winners[0] if len(winners) == 1 else None

    def resolve(self, *candidates: Optional[str]) -> Optional[str]:
        for text in candidates:
            if text:
                found = self.as_path(text) or self.as_module(text)
                if found:
                    return found
        return None


def _cases(element: ET.Element, suite: Optional[str]):
    for child in element:
        if child.tag == 'testsuite':
            yield from _cases(child, child.get('name'))
        elif child.tag == 'testcase':
            yield child, suite


def _read(path: Source) -> ET.Element:
    document = read_xml(path, 'JUnit report')
    if document.tag not in ('testsuites', 'testsuite'):
        raise ConfigError(f'JUnit report {path} is not JUnit: its root is <{document.tag}>')
    return document


def rewrite(path: Source, files: Sequence[str]) -> str:
    """The report with each case's `file` one of `files`; a case matching none loses it."""
    try:
        return _rewrite(path, files)
    except RecursionError:
        raise ConfigError(f'JUnit report {path} nests its suites too deep to read') from None


def _rewrite(path: Source, files: Sequence[str]) -> str:
    document = _read(path)
    index = _Files(files)
    lost = []
    outer = document.get('name') if document.tag == 'testsuite' else None
    for case, suite in _cases(document, outer):
        found = index.resolve(case.get('file'), case.get('classname'), case.get('name'), suite)
        if found:
            case.set('file', found)
        else:
            case.attrib.pop('file', None)
            lost.append(f'{case.get("classname") or ""}::{case.get("name") or ""}')
    if lost:
        ghio.warning(f'JUnit report {path}: no test file matches these cases, so their time'
                     f' counts for no file: {", ".join(lost)}')
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            + ET.tostring(document, encoding='unicode') + '\n')


def seconds_per_file(path: Source) -> Dict[str, float]:
    """Each `file`'s summed case `time`."""
    found: Dict[str, List[float]] = defaultdict(list)
    for case in _read(path).iter('testcase'):
        name = case.get('file')
        if not name:
            continue
        raw = case.get('time', '0')
        try:
            seconds = float(raw)
        except ValueError:
            seconds = math.nan
        if not (math.isfinite(seconds) and seconds >= 0):
            raise ConfigError(f'JUnit report {path}: case {case.get("name")} has time {raw!r},'
                              ' not a finite non-negative number of seconds')
        found[times.normalise(name)].append(seconds)
    return {name: math.fsum(values) for name, values in found.items()}
