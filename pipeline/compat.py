"""Convert TUIK GTIP-12 partitions → drop-in replacement for ``tr_full.parquet``.

The legacy ``tr_full.parquet`` (built by mgimo-foreign_trade's
``turkey_collector``/``turkey_processor``) follows the project-wide schema:

    NAPR | PERIOD | STRANA | TNVED | EDIZM | EDIZM_ISO |
    STOIM | NETTO | KOL | TNVED4 | TNVED6 | TNVED2

Conventions for Turkey:

* ``STRANA = "TR"`` — Turkey is the foreign partner in a *Russia-centric* dataset.
* ``NAPR`` is from **Russia's** perspective:
    - Turkey export to RU (``flow == "X"``)  → ``"ИМ"`` (Russia import)
    - Turkey import from RU (``flow == "M"``) → ``"ЭК"`` (Russia export)
* ``TNVED`` length defaults to 8 to match ``tr_full.parquet``. Pass
  ``--hs 12`` to keep the full GTIP-12 detail in ``TNVED`` (extra resolution).
* ``STOIM`` = USD value, ``NETTO`` = net weight in kg (``q1``), ``KOL`` =
  supplementary quantity (``q2``).
* ``EDIZM`` / ``EDIZM_ISO`` are resolved from Turkish ``OLCU_ADI`` via the
  mgimo project's ``resolve_edizm_records`` + ``edizm.csv`` map (falls back to
  a small local alias table if the sibling project is absent).

Extra columns kept on top of the legacy schema:

* ``ISTPOZ``        — original GTIP-12 code (preserved even when ``--hs 8``).
* ``ISTPOZ_ADI``    — Turkish product description from TUIK.
* ``TNVED_EN_NAME`` — best-effort English description (HS-6 from WCO).
* ``TNVED_RU_NAME`` — Russian ТНВЭД name (8-digit) if mgimo's ``tnved.csv`` is
  available; otherwise ``None``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd
from loguru import logger

from ._mgimo import (
    common_edizm_mapping,
    finalize_country_output,
    mgimo_available,
    resolve_edizm_records,
    tnved_mapping,
)
from .config import SETTINGS
from .hs_names import attach_hs_names
from .storage import register_duckdb


EXTRA_COLUMNS = ("ISTPOZ", "ISTPOZ_ADI", "TNVED_EN_NAME", "TNVED_RU_NAME")


def _load_raw(
    *,
    partner_kodu: str,
    year_from: Optional[int],
    year_to: Optional[int],
    source: str = "tuik_bi",
) -> pd.DataFrame:
    """Pull raw rows from the partitioned parquets via DuckDB."""
    con = register_duckdb(view_name="tuik_trade", source=source)
    where = ["partner_kodu = ?"]
    params: list = [partner_kodu]
    if year_from is not None:
        where.append("YIL >= ?")
        params.append(year_from)
    if year_to is not None:
        where.append("YIL <= ?")
        params.append(year_to)
    sql = f"""
        SELECT YIL, AY, IHRITH, flow, ISTPOZ, ISTPOZ_ADI, OLCU_ADI,
               usd, eur, "try", q1, q2
        FROM tuik_trade
        WHERE {" AND ".join(where)}
    """
    df = con.execute(sql, params).fetchdf()
    con.close()
    if df.empty:
        raise RuntimeError(
            f"No rows in tuik_trade for partner={partner_kodu} "
            f"year_from={year_from} year_to={year_to}"
        )
    df["ISTPOZ"] = (
        df["ISTPOZ"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(12)
    )
    return df


def _attach_tnved_ru(df: pd.DataFrame, *, tnved_col: str = "TNVED") -> pd.DataFrame:
    """Add ``TNVED_RU_NAME`` using mgimo's tnved.csv. Silent no-op if absent."""
    mapping = tnved_mapping()
    if not mapping:
        out = df.copy()
        out["TNVED_RU_NAME"] = None
        return out

    # Pick the longest matching level (8/6/4/2).
    levels = ("tnved8", "tnved6", "tnved4", "tnved2")
    table: dict[str, str] = {}
    for lvl in levels:
        for code, rec in mapping.get(lvl, {}).items():
            name = rec.get("name") if isinstance(rec, dict) else None
            if name and code not in table:
                table[code] = name

    def _resolve(code: object) -> Optional[str]:
        if code is None or (isinstance(code, float) and pd.isna(code)):
            return None
        s = str(code).strip()
        for n in (len(s), 8, 6, 4, 2):
            if n > len(s):
                continue
            v = table.get(s[:n])
            if v:
                return v
        return None

    out = df.copy()
    out["TNVED_RU_NAME"] = out[tnved_col].map(_resolve)
    return out


def to_old_schema(
    df: pd.DataFrame,
    *,
    hs_level: int = 8,
    country_code: str = "TR",
    aggregate: bool = True,
) -> pd.DataFrame:
    """Transform GTIP-12 rows into the mgimo country-output contract.

    Parameters
    ----------
    df
        Raw rows from ``_load_raw`` (one row per GTIP-12 / month / flow).
    hs_level
        Output length of ``TNVED``. ``8`` matches ``tr_full.parquet`` exactly;
        ``10``/``12`` keep more Turkey-specific detail.
    country_code
        Value for the ``STRANA`` column (partner from RU's viewpoint).
    aggregate
        When ``True`` (default), SUM rows that collapse to the same
        ``(PERIOD, NAPR, TNVED, EDIZM_ISO)`` key. With ``--hs 12`` aggregation
        is mostly a no-op because GTIP-12 is already the cube's grain.
    """
    if hs_level not in (6, 8, 10, 12):
        raise ValueError(f"hs_level must be one of 6,8,10,12 (got {hs_level})")

    df = df.copy()

    # 1) PERIOD: first day of month, datetime64[ns]
    df["PERIOD"] = pd.to_datetime(
        df["YIL"].astype(str) + "-" + df["AY"].astype(str).str.zfill(2) + "-01",
        errors="coerce",
    )

    # 2) NAPR (Russia-centric)
    #   "X" (Turkey export to RU) → "ИМ" (Russia import)
    #   "M" (Turkey import from RU) → "ЭК" (Russia export)
    df["NAPR"] = df["flow"].map({"X": "ИМ", "M": "ЭК"}).fillna(df["flow"])

    # 3) STRANA
    df["STRANA"] = country_code

    # 4) TNVED truncated to requested HS level (keep original ISTPOZ separately)
    df["TNVED"] = df["ISTPOZ"].str[:hs_level]

    # 5) Money/quantity — keep float64 to match tr_full.parquet dtypes exactly.
    df["STOIM"] = pd.to_numeric(df["usd"], errors="coerce").astype("float64")
    df["NETTO"] = pd.to_numeric(df["q1"], errors="coerce").astype("float64")
    df["KOL"] = pd.to_numeric(df["q2"], errors="coerce").astype("float64")

    # 6) EDIZM / EDIZM_ISO from OLCU_ADI
    edizm_map = common_edizm_mapping() if mgimo_available() else {}
    records = resolve_edizm_records(df["OLCU_ADI"], edizm_map)
    df["EDIZM"] = records.map(lambda r: r.get("NAME") if isinstance(r, dict) else None)
    df["EDIZM_ISO"] = records.map(lambda r: r.get("KOD") if isinstance(r, dict) else None)

    # 7) Aggregate to the contract key so STOIM/NETTO/KOL are correct sums.
    group_cols = ["NAPR", "PERIOD", "STRANA", "TNVED", "EDIZM", "EDIZM_ISO"]
    if aggregate:
        df = (
            df.groupby(group_cols, dropna=False, as_index=False)
            .agg(
                STOIM=("STOIM", "sum"),
                NETTO=("NETTO", "sum"),
                KOL=("KOL", "sum"),
                ISTPOZ=("ISTPOZ", "first"),
                ISTPOZ_ADI=("ISTPOZ_ADI", "first"),
            )
        )

    # 8) Build the extras lookup BEFORE finalize (finalize sorts/dedups, which
    # would break positional alignment). Merge key includes EDIZM_ISO so that
    # multi-unit codes don't collide.
    extras_key = ["NAPR", "PERIOD", "STRANA", "TNVED", "EDIZM_ISO"]
    extras = (
        df[extras_key + ["ISTPOZ", "ISTPOZ_ADI"]]
        .drop_duplicates(subset=extras_key, keep="first")
    )

    # 9) Finalize via the mgimo contract — guarantees:
    #    * NAPR normalisation, PERIOD as datetime
    #    * STRANA uppercased
    #    * TNVED 2/4/6 derived columns
    #    * numeric coercion of STOIM/NETTO/KOL
    #    * sort + drop_duplicates on contract cols
    finalized = finalize_country_output(
        df.drop(columns=["ISTPOZ", "ISTPOZ_ADI"], errors="ignore"),
        country_code=country_code,
    )

    # 10) Re-attach extras by the contract key. left-merge keeps row order.
    finalized = finalized.merge(extras, on=extras_key, how="left")

    # 11) Attach English HS descriptions (HS-6 level fallback for codes >6)
    finalized = attach_hs_names(finalized, tnved_col="TNVED", out_col="TNVED_EN_NAME")

    # 12) Attach Russian TNVED names (if mgimo metadata available)
    finalized = _attach_tnved_ru(finalized, tnved_col="TNVED")

    return finalized


def compat_export(
    *,
    partner_kodu: str = "75",
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    hs_level: int = 8,
    country_code: str = "TR",
    out: Optional[Path] = None,
    aggregate: bool = True,
) -> Path:
    """End-to-end: read partitions → old-schema parquet at ``out``."""
    raw = _load_raw(
        partner_kodu=partner_kodu,
        year_from=year_from,
        year_to=year_to,
    )
    logger.info("compat-export: {:,} raw GTIP-12 rows loaded", len(raw))

    out_df = to_old_schema(
        raw,
        hs_level=hs_level,
        country_code=country_code,
        aggregate=aggregate,
    )

    if out is None:
        suffix = f"_hs{hs_level}" if hs_level != 8 else ""
        out = SETTINGS.data_dir / "exports" / f"tr_full_compat{suffix}.parquet"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_parquet(out, index=False)

    logger.info(
        "compat-export: {:,} rows -> {} (mgimo_available={})",
        len(out_df), out, mgimo_available(),
    )
    return out
