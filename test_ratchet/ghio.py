"""Step outputs, step summaries and log annotations for every entry point."""
import os
import uuid

from test_ratchet import ConfigError


def _append(variable: str, text: str) -> None:
    path = os.environ.get(variable)
    if not path:
        raise ConfigError(f'{variable} is not set: run this inside a GitHub Actions step')
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(text)


def set_output(name: str, value: str) -> None:
    if '\n' in value or '\r' in value:
        delimiter = f'ghadelimiter_{uuid.uuid4()}'
        _append('GITHUB_OUTPUT', f'{name}<<{delimiter}\n{value}\n{delimiter}\n')
    else:
        _append('GITHUB_OUTPUT', f'{name}={value}\n')


def add_summary(markdown: str) -> None:
    _append('GITHUB_STEP_SUMMARY', markdown if markdown.endswith('\n') else markdown + '\n')


def _annotate(kind: str, message: str) -> None:
    escaped = message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    print(f'::{kind}::{escaped}', flush=True)


def notice(message: str) -> None:
    _annotate('notice', message)


def warning(message: str) -> None:
    _annotate('warning', message)
