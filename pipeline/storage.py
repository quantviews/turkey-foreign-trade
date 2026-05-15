"""Partitioned Parquet storage + DuckDB registration."""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from loguru import logger

from .config import SETTINGS, TRADE_SYSTEM


def write_partition(
    df: pd.DataFrame,
    partner_kodu: str,
    year: int,
    month: int,
    *,
    source: str = "tuik_bi",
    trade_system: str = TRADE_SYSTEM,
    overwrite: bool = True,
) -> Path:
    """Write one partition Parquet at data/raw/<source>/partner_kodu=K/year=Y/month=M.parquet."""
    base = SETTINGS.raw_dir / source / f"partner_kodu={partner_kodu}" / f"year={year}"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"month={month:02d}.parquet"
    if path.exists() and not overwrite:
        logger.info(f"skip (exists) {path}")
        return path

    df = df.copy()
    if "trade_system" not in df.columns:
        df["trade_system"] = trade_system

    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(
        table,
        path,
        compression="zstd",
        compression_level=9,
    )
    logger.info(f"wrote {len(df):>7d} rows -> {path}")
    return path


def partition_exists(partner_kodu: str, year: int, month: int, source: str = "tuik_bi") -> bool:
    path = (
        SETTINGS.raw_dir
        / source
        / f"partner_kodu={partner_kodu}"
        / f"year={year}"
        / f"month={month:02d}.parquet"
    )
    return path.exists()


def register_duckdb(view_name: str = "tuik_trade", source: str = "tuik_bi") -> duckdb.DuckDBPyConnection:
    """Open data/trade.duckdb and register a partitioned view on all parquet files."""
    con = duckdb.connect(str(SETTINGS.duckdb_path))
    glob = str(SETTINGS.raw_dir / source / "**" / "*.parquet").replace("\\", "/")
    con.execute(f"""
        CREATE OR REPLACE VIEW {view_name} AS
        SELECT * FROM read_parquet('{glob}', hive_partitioning = TRUE);
    """)
    return con
