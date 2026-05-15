"""Pre-baked DuckDB analytics over the partitioned Parquet store."""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from .config import SETTINGS

VIEW = "tuik_trade"


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(SETTINGS.duckdb_path), read_only=read_only)
    glob = str(SETTINGS.raw_dir / "tuik_bi" / "**" / "*.parquet").replace("\\", "/")
    con.execute(f"""
        CREATE OR REPLACE VIEW {VIEW} AS
        SELECT * FROM read_parquet('{glob}', hive_partitioning = TRUE);
    """)
    return con


def monthly_totals(partner_kodu: str = "75") -> pd.DataFrame:
    """Aggregate by year-month-flow (USD/EUR/TRY)."""
    con = connect()
    return con.execute(f"""
        SELECT
          YIL                       AS year,
          AY                        AS month,
          flow,
          SUM(usd)                  AS usd,
          SUM(eur)                  AS eur,
          SUM("try")                AS "try",
          COUNT(DISTINCT ISTPOZ)    AS n_gtip,
          SUM(q1)                   AS qty1,
          SUM(q2)                   AS qty2
        FROM {VIEW}
        WHERE partner_kodu = ?
        GROUP BY YIL, AY, flow
        ORDER BY YIL, AY, flow
    """, [partner_kodu]).fetchdf()


def yearly_totals(partner_kodu: str = "75") -> pd.DataFrame:
    con = connect()
    return con.execute(f"""
        SELECT
          YIL          AS year,
          flow,
          SUM(usd)     AS usd,
          SUM(eur)     AS eur,
          SUM("try")   AS "try"
        FROM {VIEW}
        WHERE partner_kodu = ?
        GROUP BY YIL, flow
        ORDER BY YIL, flow
    """, [partner_kodu]).fetchdf()


def top_products(
    partner_kodu: str = "75",
    year: int | None = None,
    flow: str | None = None,
    n: int = 20,
    hs_level: int = 12,
) -> pd.DataFrame:
    """Top N products by USD trade value.

    hs_level: 2|4|6|8|12 (12 = full GTIP).
    """
    code_expr = f"LEFT(ISTPOZ, {hs_level})" if hs_level < 12 else "ISTPOZ"
    where = ["partner_kodu = ?"]
    params: list = [partner_kodu]
    if year is not None:
        where.append("YIL = ?")
        params.append(year)
    if flow:
        where.append("flow = ?")
        params.append(flow)
    where_sql = " AND ".join(where)

    con = connect()
    return con.execute(f"""
        SELECT
          {code_expr} AS hs_code,
          ANY_VALUE(ISTPOZ_ADI) AS description,
          flow,
          SUM(usd) AS usd,
          SUM("try") AS "try"
        FROM {VIEW}
        WHERE {where_sql}
        GROUP BY hs_code, flow
        ORDER BY usd DESC
        LIMIT {n}
    """, params).fetchdf()


def to_csv(df: pd.DataFrame, name: str) -> Path:
    out_dir = SETTINGS.data_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / name
    df.to_csv(out, index=False, encoding="utf-8-sig")
    return out
