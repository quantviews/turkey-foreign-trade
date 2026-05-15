"""Flatten Qlik hypercube payloads to tidy DataFrames."""
from __future__ import annotations

from typing import Any

import pandas as pd

from .config import DEFAULT_MEASURES


# Map Turkish IHRITH labels to canonical English flow codes.
FLOW_MAP = {
    "İhracat": "X",   # Export
    "Ihracat": "X",
    "İthalat": "M",   # Import
    "Ithalat": "M",
    "Export": "X",
    "Import": "M",
}


def cube_to_dataframe(
    cube: dict[str, Any],
    dim_columns: list[str],
    measures: list[dict[str, str]] | None = None,
) -> pd.DataFrame:
    """Convert a TuikBI.query() payload into a DataFrame.

    Parameters
    ----------
    cube
        Output of TuikBI.query().
    dim_columns
        Logical column names for the dimensions, in the same order as the
        ``dims`` argument that was passed to ``query``.
    measures
        Measure spec list with ``id``/``label`` keys, matching the order
        passed to ``query``. Defaults to :data:`DEFAULT_MEASURES`.
    """
    measures = measures or DEFAULT_MEASURES
    meas_columns = [m["id"] for m in measures]
    columns = list(dim_columns) + meas_columns

    matrix = cube.get("matrix", [])
    if not matrix:
        return pd.DataFrame(columns=columns)

    rows: list[list[Any]] = []
    for row in matrix:
        out: list[Any] = []
        for i, _ in enumerate(dim_columns):
            cell = row[i]
            out.append(cell.get("t"))
        for j, _ in enumerate(meas_columns):
            cell = row[len(dim_columns) + j]
            out.append(cell.get("n"))
        rows.append(out)

    df = pd.DataFrame(rows, columns=columns)

    # Type coercions
    if "YIL" in df.columns:
        df["YIL"] = pd.to_numeric(df["YIL"], errors="coerce").astype("Int32")
    if "AY" in df.columns:
        df["AY"] = pd.to_numeric(df["AY"], errors="coerce").astype("Int8")
    if "IHRITH" in df.columns:
        df["flow"] = df["IHRITH"].map(FLOW_MAP).fillna(df["IHRITH"])
    if "ISTPOZ" in df.columns:
        # Qlik strips leading zeros from numeric-looking codes (e.g. HS chapter
        # 01..09 → 11 chars instead of 12). Zero-pad to canonical 12 digits.
        df["ISTPOZ"] = (
            df["ISTPOZ"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(12)
        )
    for m in meas_columns:
        df[m] = pd.to_numeric(df[m], errors="coerce")

    return df
