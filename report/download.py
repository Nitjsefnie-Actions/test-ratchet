import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_ratchet import download  # noqa: E402

if __name__ == '__main__':
    sys.exit(download.main())
