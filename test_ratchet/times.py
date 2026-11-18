"""The committed per-file times file: {"schema_version": 1, "cells": {cell: {path: s}}}."""
import json
import math
from pathlib import Path
from typing import Dict, Optional, Union

from test_ratchet import ConfigError, ghio

Cells = Dict[str, Dict[str, float]]


def normalise(path: str) -> str:
    """Repository-relative POSIX form: `\\` becomes `/`, leading `./` dropped."""
    path = path.replace('\\', '/')
    while path.startswith('./'):
        path = path[2:]
    return path


def _seconds(path: object, cell: str, key: str, value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            seconds = float(value)
        except OverflowError:
            seconds = math.inf
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    raise ConfigError(f'times file {path}: key {key} in cell {cell} must be a finite'
                      f' non-negative number of seconds, got {value!r}')


def load(path: Optional[Union[str, Path]]) -> Cells:
    if not path:
        return {}
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.loads(handle.read())
    except FileNotFoundError:
        ghio.notice(f'times file {path} does not exist: no recorded times')
        return {}
    except UnicodeDecodeError as error:
        raise ConfigError(f'times file {path} is not UTF-8: {error}') from error
    except ValueError as error:
        raise ConfigError(f'times file {path} is not JSON: {error}') from error
    except OSError as error:
        raise ConfigError(f'times file {path} cannot be read: {error}') from error
    version = data.get('schema_version') if isinstance(data, dict) else None
    if type(version) is not int or version != 1:
        raise ConfigError(f'times file {path}: key schema_version must be 1')
    cells = data.get('cells')
    if not isinstance(cells, dict):
        raise ConfigError(f'times file {path}: key cells must be an object')
    result: Cells = {}
    for cell, records in cells.items():
        if not isinstance(records, dict):
            raise ConfigError(f'times file {path}: key cells.{cell} must be an object')
        result[cell] = {}
        for key, value in records.items():
            name = normalise(key)
            if name in result[cell]:
                raise ConfigError(f'times file {path}: key {key} in cell {cell} repeats {name}')
            result[cell][name] = _seconds(path, cell, key, value)
    return result


def dump(cells: Cells) -> str:
    body = {cell: {name: round(seconds, 2) for name, seconds in sorted(records.items())}
            for cell, records in sorted(cells.items())}
    return json.dumps({'schema_version': 1, 'cells': body}, indent=2) + '\n'
