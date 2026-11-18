"""Cobertura coverage reports as {repo-relative path: {line: hits}}: read, written, merged."""
import os
import posixpath
import xml.etree.ElementTree as ET
from typing import Dict, Iterable, List, Optional

from test_ratchet import ConfigError, ghio, paths
from test_ratchet.xmlio import Source, read_xml

Lines = Dict[str, Dict[int, int]]


def _number(path: Source, filename: str, value: Optional[str], least: int) -> int:
    try:
        number = int(value or '')
    except ValueError:
        number = least - 1
    if number < least:
        raise ConfigError(f'coverage report {path}: file {filename} has a line with'
                          f' invalid number or hits {value!r}')
    return number


def _resolve(filename: str, sources: List[str], root: str) -> Optional[str]:
    if paths.is_absolute(filename) or not sources:
        return paths.repo_relative(filename, root)
    found = {paths.repo_relative(posixpath.join(source.replace('\\', '/'),
                                                filename.replace('\\', '/')), root)
             for source in sources}
    found.discard(None)
    if len(sources) > 1:
        # A filename is relative to one of several sources: only the disk tells which.
        held = sorted(name for name in found if name and os.path.isfile(os.path.join(root, name)))
        if len(held) != 1:
            ghio.warning(f'coverage file {filename} dropped: {"several" if held else "no"}'
                         f' of the report\'s sources hold it ({", ".join(held or sources)})')
            return None
        return held[0]
    return found.pop() if found else None


def parse(path: Source, root: Source) -> Lines:
    """Line hits per file under `root`; paths resolve against the report's <source>s."""
    document = read_xml(path, 'coverage report')
    if document.tag != 'coverage':
        raise ConfigError(f'coverage report {path} is not Cobertura: its root is'
                          f' <{document.tag}>')
    sources = [source.text.strip() for source in document.iter('source')
               if source.text and source.text.strip()]
    result: Lines = {}
    for item in document.iter('class'):
        filename = item.get('filename')
        if not filename:
            raise ConfigError(f'coverage report {path}: a <class> has no filename')
        name = _resolve(filename, sources, str(root))
        lines = {}
        for line in item.findall('lines/line'):
            number = _number(path, filename, line.get('number'), 1)
            lines[number] = _number(path, filename, line.get('hits'), 0)
        if name is not None:
            known = result.setdefault(name, {})
            for number, hits in lines.items():
                known[number] = max(hits, known.get(number, 0))
    return result


def _rate(covered: int, valid: int) -> str:
    return f'{covered / valid:.4g}' if valid else '1'


def dump(lines: Lines) -> str:
    """The Cobertura document for `lines`: one <class> per file under source `.`."""
    covered = sum(1 for hits in lines.values() for count in hits.values() if count)
    valid = sum(len(hits) for hits in lines.values())
    common = {'branch-rate': '0', 'complexity': '0'}
    document = ET.Element('coverage', {
        'version': 'test-ratchet', 'timestamp': '0', 'lines-valid': str(valid),
        'lines-covered': str(covered), 'line-rate': _rate(covered, valid),
        'branches-valid': '0', 'branches-covered': '0', **common})
    ET.SubElement(ET.SubElement(document, 'sources'), 'source').text = '.'
    package = ET.SubElement(ET.SubElement(document, 'packages'), 'package',
                            {'name': '.', 'line-rate': _rate(covered, valid), **common})
    classes = ET.SubElement(package, 'classes')
    for name in sorted(lines):
        hits = lines[name]
        item = ET.SubElement(classes, 'class', {
            'name': name, 'filename': name,
            'line-rate': _rate(sum(1 for count in hits.values() if count), len(hits)), **common})
        ET.SubElement(item, 'methods')
        listed = ET.SubElement(item, 'lines')
        for number in sorted(hits):
            ET.SubElement(listed, 'line', {'number': str(number), 'hits': str(hits[number])})
    ET.indent(document)
    return '<?xml version="1.0" ?>\n' + ET.tostring(document, encoding='unicode') + '\n'


def merge(reports: Iterable[Lines]) -> Lines:
    """Every input's lines; a line's hits are the sum, so any covering input covers it."""
    merged: Lines = {}
    for report in reports:
        for name, hits in report.items():
            known = merged.setdefault(name, {})
            for number, count in hits.items():
                known[number] = known.get(number, 0) + count
    return merged
