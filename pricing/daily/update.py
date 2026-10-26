"""DAILY workflow: bring in new pricing as it becomes available. Owner only (needs Bloomberg and a Databricks login).

    python pricing/daily/update.py            # pull new dates for all 40 indices into the working copy cache/
    python pricing/daily/update.py HY47 IG47  # or only some indices

After it: `python pricing/shared/cds_pricer.py` (re-price at the index coupon, system Python), then `python pricing/shared/publish.py`.
This is separate from pricing/fix_history/, which repairs OLD missing data; see CLAUDE.md for the planned base / live split that will stop this step touching old dates.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared'))     # find _paths (it adds the other pricing folders and analysis/)
import _paths  # noqa: F401
import cdx_store

if __name__ == '__main__':
    cdx_store.update_store(sys.argv[1:] or None)
