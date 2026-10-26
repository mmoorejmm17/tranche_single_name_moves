"""Import this FIRST in every pricing module.

Pricing works on the WORKING copy (cache/ and config/); the analysis modules read the published snapshot (data/). Both use the same analysis/cdx_spreads.py,
so this points it at the working copy through two env vars before it is imported, and makes analysis/ importable. publish.py is what copies cache/ -> data/.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / 'cache'                  # pricing working area
STORE = CACHE / 'base'                  # working copy of the store 'base' (prices/ reference/ metrics/)
CONFIG = ROOT / 'config'
DATA = ROOT / 'data' / 'base'           # the published store

os.environ['CDS_DATA_DIR'] = str(STORE)
os.environ['CDS_CONFIG_DIR'] = str(CONFIG)
for _p in (ROOT / "analysis", ROOT / "pricing" / "shared", ROOT / "pricing" / "fix_history", ROOT / "pricing" / "daily"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_cs = sys.modules.get('cdx_spreads')
if _cs is not None and Path(_cs.DATA_DIR).resolve() != STORE.resolve():
    raise RuntimeError('cdx_spreads was imported before pricing/_paths.py and points at the published data/; import _paths first (restart the kernel).')
