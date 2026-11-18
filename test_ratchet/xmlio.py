"""Reading XML reports."""
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Union
from xml.parsers import expat

from test_ratchet import ConfigError

Source = Union[str, Path]


def _refuse_entities(*_: object) -> None:
    raise ValueError('the document declares an entity')


def read_xml(path: Source, kind: str) -> ET.Element:
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise ConfigError(f'{kind} {path} cannot be read: {error}') from error
    try:
        # Reports can come from untrusted runs: refuse entity declarations (expansion bombs).
        guard = expat.ParserCreate()
        guard.EntityDeclHandler = _refuse_entities
        guard.Parse(data, True)
        return ET.fromstring(data)
    except (ValueError, expat.ExpatError, ET.ParseError) as error:
        raise ConfigError(f'{kind} {path} is not valid XML: {error}') from error
