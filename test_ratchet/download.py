"""The artifacts `report` reads: of each name the one created last, downloaded by its ID."""
import hashlib
import io
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List

from test_ratchet import ConfigError, github
from test_ratchet.report import env_input

PREFIX = 'test-ratchet-'
# As upload-artifact does: a name is one path component and one line.
REFUSED = re.compile(r'["\\/:<>|*?\r\n]')


def latest(listed: List[Any]) -> List[Dict[str, Any]]:
    found = {}  # by creation, which IDs do not follow
    for item in sorted((item for item in listed if item['name'].startswith(PREFIX)),
                       key=lambda item: item['created_at'] or ''):
        found[item['name']] = item
    return list(found.values())


def fetch(client: github.Client, repo: str, item: Dict[str, Any], folder: Path) -> None:
    name, number, digest = item['name'], item['id'], item.get('digest')
    # shortcut: held in memory, stream it once an artifact outgrows it.
    data = client.download(f'repos/{repo}/actions/artifacts/{number}/zip')
    if digest and digest != f'sha256:{hashlib.sha256(data).hexdigest()}':
        raise ConfigError(f'artifact {name} (ID {number}) does not have its digest {digest}')
    folder.mkdir(exist_ok=True)
    if zipfile.is_zipfile(io.BytesIO(data)):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                archive.extractall(folder)
        except Exception as error:  # pylint: disable=broad-exception-caught
            raise ConfigError(f'artifact {name} (ID {number}) cannot be unpacked: {error}') \
                from None
    else:
        # Uploaded unarchived: report refuses it.
        (folder / name).write_bytes(data)
    print(f'test-ratchet report: downloaded {name} (ID {number}, created {item["created_at"]})')


def main() -> int:
    root, repo = Path(env_input('ARTIFACTS')), os.environ.get('GITHUB_REPOSITORY', '')
    client = github.Client(env_input('GITHUB_TOKEN'))
    try:
        found = latest(client.pages(f'repos/{repo}/actions/runs/{env_input("RUN_ID")}/artifacts'
                                    '?per_page=100', 'artifacts'))
        for item in found:
            if REFUSED.search(item['name']):
                raise ConfigError(f'{item["name"]!r} is no artifact name upload-artifact accepts')
            fetch(client, repo, item, root if len(found) == 1 else root / item['name'])
    except ConfigError as error:
        print(f'test-ratchet report: {error}', file=sys.stderr)
        return 2
    return 0
