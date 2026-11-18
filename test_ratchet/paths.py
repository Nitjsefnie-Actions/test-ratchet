"""Repository-relative POSIX paths from the paths coverage tools write."""
import os
import posixpath
import re
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

_DRIVE = re.compile(r'[A-Za-z]:/')


def _posix(path: str) -> str:
    path = path.replace('\\', '/')
    if _DRIVE.match(path):
        path = path[0].upper() + path[1:]
    return posixpath.normpath(path)


def is_absolute(path: str) -> bool:
    path = path.replace('\\', '/')
    return path.startswith('/') or bool(_DRIVE.match(path))


def _roots(root: str) -> List[str]:
    native = root if is_absolute(root) else os.path.abspath(root)
    found = [_posix(native)]
    if os.path.isabs(native):
        found.append(_posix(os.path.realpath(native)))
    return found


def repo_relative(path: str, root: str) -> Optional[str]:
    """`path` relative to `root`, or None outside it."""
    roots = _roots(root)
    if not is_absolute(path):
        path = posixpath.join(roots[0], path.replace('\\', '/'))
    path = _posix(path)
    for prefix in roots:
        if path.startswith(prefix.rstrip('/') + '/'):
            return path[len(prefix.rstrip('/')) + 1:]
    return None


def _shared_tail(left: Sequence[str], right: Sequence[str]) -> int:
    count = 0
    while count < min(len(left), len(right)) and left[-1 - count] == right[-1 - count]:
        count += 1
    return count


class Index:
    """Repository paths by basename, matched against a path's trailing segments."""

    def __init__(self, files: Iterable[str]):
        self.by_name: Dict[str, List[str]] = defaultdict(list)
        for path in files:
            self.by_name[posixpath.basename(path)].append(path)

    def match(self, segments: Sequence[str]) -> Tuple[Optional[str], List[str]]:
        """The path sharing most trailing segments, a tie going to one that is wholly
        shared; else None and the tied paths."""
        named = self.by_name.get(segments[-1], ()) if segments else ()
        return _best(segments, named)


def _best(segments: Sequence[str], candidates: Iterable[str]) -> Tuple[Optional[str], List[str]]:
    scored = []
    for candidate in candidates:
        parts = candidate.split('/')
        shared = _shared_tail(segments, parts)
        if shared:
            scored.append(((shared, shared == len(parts)), candidate))
    if not scored:
        return None, []
    best = max(score for score, _ in scored)
    tied = sorted(candidate for score, candidate in scored if score == best)
    return (tied[0], []) if len(tied) == 1 else (None, tied)
