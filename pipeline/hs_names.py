"""HS code → English description lookup.

GTIP-12 is Turkey-specific (8-digit CN8 + 4 national digits). Only the first
6 digits (HS-6) are internationally harmonised, so that is the level at which
we resolve English descriptions reliably.

Sources, in order of preference:

1. **HS-6 (WCO)** — `datasets/harmonized-system` GitHub CSV (~5.6k codes, EN).
   Downloaded lazily into ``data/refs/hs6_en.csv``; cached forever.
2. **CN8 (EU)** — *not implemented yet*. The Eurostat RAMON service publishes
   yearly CN8 nomenclature, but the export endpoint is JS-heavy and changes
   names every release. A scraping path is left as a TODO. For now, CN8 codes
   fall back to their HS-6 parent.

Returned table:

    hs_code (str), level (int: 2|4|6), name_en (str)
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd
import requests
from loguru import logger

from .config import SETTINGS

HS6_URL = (
    "https://raw.githubusercontent.com/datasets/harmonized-system/master/"
    "data/harmonized-system.csv"
)
HS6_CACHE = SETTINGS.refs_dir / "hs6_en.csv"


def _download_hs6(dest: Path) -> None:
    logger.info("Downloading HS-6 EN reference: {}", HS6_URL)
    resp = requests.get(HS6_URL, timeout=60)
    resp.raise_for_status()
    dest.write_bytes(resp.content)


def load_hs6_table(refresh: bool = False) -> pd.DataFrame:
    """Return the cached HS reference table with columns: hscode, description, level."""
    if refresh or not HS6_CACHE.exists():
        _download_hs6(HS6_CACHE)
    df = pd.read_csv(HS6_CACHE, dtype={"hscode": str})
    df["hscode"] = df["hscode"].astype(str).str.strip()
    # Source uses 1/2/4/6-digit codes mixed; derive level from string length.
    df["level"] = df["hscode"].str.len()
    return df


def build_hs_lookup(refresh: bool = False) -> dict[str, str]:
    """Return ``{hs_code: en_description}`` covering levels 2, 4, 6.

    The source CSV has codes at all granularities. Keys preserve leading
    zeros (string keys).
    """
    df = load_hs6_table(refresh=refresh)
    name_col = "description" if "description" in df.columns else df.columns[1]
    return dict(zip(df["hscode"].astype(str), df[name_col].astype(str)))


def attach_hs_names(
    df: pd.DataFrame,
    *,
    tnved_col: str = "TNVED",
    out_col: str = "TNVED_EN_NAME",
    refresh: bool = False,
) -> pd.DataFrame:
    """Add a column with the best-available English HS description.

    Resolution order per code:

    1. Exact match at the input length (so an 8-digit TNVED would match an
       8-digit entry — but the WCO source only has up to HS-6, so this rarely
       fires).
    2. HS-6 parent (``code[:6]``).
    3. HS-4 parent.
    4. HS-2 parent.
    5. ``None``.
    """
    lookup = build_hs_lookup(refresh=refresh)
    if tnved_col not in df.columns:
        df = df.copy()
        df[out_col] = None
        return df

    def _resolve(code: object) -> str | None:
        if code is None or (isinstance(code, float) and pd.isna(code)):
            return None
        s = str(code).strip()
        for n in (len(s), 6, 4, 2):
            if n > len(s):
                continue
            v = lookup.get(s[:n])
            if v:
                return v
        return None

    out = df.copy()
    out[out_col] = out[tnved_col].map(_resolve)
    return out


def hs_codes_missing(df: pd.DataFrame, tnved_col: str = "TNVED") -> list[str]:
    """Diagnostic: which HS codes in df had no EN description at any level."""
    lookup = build_hs_lookup()
    missing: set[str] = set()
    for code in df[tnved_col].dropna().unique():
        s = str(code).strip()
        if not any(lookup.get(s[:n]) for n in (len(s), 6, 4, 2) if n <= len(s)):
            missing.add(s)
    return sorted(missing)


def resolve_codes(codes: Iterable[str], refresh: bool = False) -> dict[str, str]:
    """Convenience: ``{code: description}`` for a list of codes."""
    lookup = build_hs_lookup(refresh=refresh)
    out: dict[str, str] = {}
    for c in codes:
        s = str(c).strip()
        for n in (len(s), 6, 4, 2):
            if n > len(s):
                continue
            v = lookup.get(s[:n])
            if v:
                out[s] = v
                break
    return out
