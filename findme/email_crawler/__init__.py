# Email Crawler module — add internal dir to sys.path for legacy imports
import sys
from pathlib import Path as _Path

_ec_dir = str(_Path(__file__).parent)
if _ec_dir not in sys.path:
    sys.path.insert(0, _ec_dir)
