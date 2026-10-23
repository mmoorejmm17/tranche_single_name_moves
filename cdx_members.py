"""CDX index -> single-name constituents table (Bloomberg INDX_MEMBERS + CDS mappings file)."""
import sys

import pandas as pd

# own utils: not pip-installed, so add the repo root to sys.path
BBG_UTILS_PATH = 'S:/Structured Credit/Matt Stuff/claude_repos/bbg_utils'
if BBG_UTILS_PATH not in sys.path:
    sys.path.insert(0, BBG_UTILS_PATH)
from bbg_utils import bds_mm

MAPPINGS_PATH = 'S:/Structured Credit/Matt Stuff/cds_mappings/cds_mappings_final.csv'

# BDS columns come back generic; names inferred from the data.
_COLMAP = {
    'Member Ticker and Exchange Code': 'member_name',
    'column   1': 'coupon_or_weight',
    'column   2': 'currency',
    'column   3': 'seniority',
    'column   4': 'bbg_cds_ticker',
    'column   5': 'sp_id',
}


def load_cds_mappings(path=MAPPINGS_PATH):
    """bbg_cds_ticker -> Market_CDS_Ticker lookup (BBG_Short_Ticker -> CDS_Ticker in the mappings file)."""
    return (pd.read_csv(path, usecols=['CDS_Ticker', 'BBG_Short_Ticker'],
                        dtype=str, encoding='cp1252')  # file isn't utf-8
            .dropna(subset=['BBG_Short_Ticker'])
            .drop_duplicates('BBG_Short_Ticker')
            .rename(columns={'BBG_Short_Ticker': 'bbg_cds_ticker', 'CDS_Ticker': 'Market_CDS_Ticker'}))


def get_index_members(index_ticker, mappings_path=MAPPINGS_PATH):
    """Constituents of a CDX index, e.g. get_index_members('CDX IG CDSI GEN 5Y Corp').

    Columns: index_ticker, member_name, coupon_or_weight, currency, seniority,
    bbg_cds_ticker, sp_id, Market_CDS_Ticker (NaN where the mappings file has no match).
    Requires a running Bloomberg Terminal session.
    """
    members = (bds_mm(index_ticker, 'INDX_MEMBERS')
               .rename(columns=_COLMAP)
               .rename(columns={'security': 'index_ticker'}))
    return (members
            .merge(load_cds_mappings(mappings_path), how='left', on='bbg_cds_ticker')
            .reset_index(drop=True))
