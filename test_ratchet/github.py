"""The GitHub REST API over urllib: the job's token, never a cached answer, every page."""
import http.client
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, List, Optional, Tuple

from test_ratchet import ConfigError

FAILURES = (OSError, http.client.HTTPException)
NEXT = re.compile(r'<([^<>]+)>;\s*rel="next"')


class ApiError(ConfigError):
    """An HTTP error answer, its status kept."""

    def __init__(self, method: str, url: str, status: int, detail: str) -> None:
        super().__init__(f'GitHub API {method} {url} answered {status}: {detail}')
        self.status = status


class Client:
    def __init__(self, token: str) -> None:
        self.token = token
        self.base = (os.environ.get('GITHUB_API_URL') or 'https://api.github.com').rstrip('/')

    def _open(self, method: str, url: str, body: Any = None) -> Tuple[bytes, str]:
        request = urllib.request.Request(
            url, method=method, data=None if body is None else json.dumps(body).encode(),
            headers={'Accept': 'application/vnd.github+json', 'Cache-Control': 'no-cache',
                     'Content-Type': 'application/json', 'User-Agent': 'test-ratchet',
                     'X-GitHub-Api-Version': '2022-11-28'})
        # Kept off any request a redirect makes, so the token never leaves for another host.
        request.add_unredirected_header('Authorization', f'Bearer {self.token}')
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read(), response.headers.get('Link') or ''
        except urllib.error.HTTPError as error:
            try:
                with error:
                    detail = error.read(300).decode('utf-8', 'replace')
            except FAILURES as cut:
                detail = str(cut)
            raise ApiError(method, url, error.code, detail) from None
        except FAILURES as error:
            raise ConfigError(f'GitHub API {method} {url} failed: {error}') from None

    def _send(self, method: str, url: str, body: Any = None) -> Tuple[Any, Optional[str]]:
        data, link = self._open(method, url, body)
        try:
            answer = json.loads(data) if data else None
        except ValueError:
            raise ConfigError(f'GitHub API {method} {url} answered no JSON') from None
        found = NEXT.search(link)
        return answer, found.group(1) if found else None

    def call(self, method: str, path: str, body: Any = None) -> Any:
        return self._send(method, f'{self.base}/{path}', body)[0]

    def download(self, path: str) -> bytes:
        """The bytes `path` answers, its redirect to another host followed."""
        return self._open('GET', f'{self.base}/{path}')[0]

    def pages(self, path: str, key: str = '') -> List[Any]:
        """Every item of a listing, or of the one each page has under `key`, following its Link
        header within this API."""
        items: List[Any] = []
        url: Optional[str] = f'{self.base}/{path}'
        while url:
            if not url.startswith(f'{self.base}/'):
                raise ConfigError(f'GitHub API {path}: the next page {url} is not under'
                                  f' {self.base}')
            page, url = self._send('GET', url)
            if key and isinstance(page, dict):
                page = page.get(key)
            if not isinstance(page, list):
                raise ConfigError(f'GitHub API GET {path} answered no list')
            items += page
        return items
