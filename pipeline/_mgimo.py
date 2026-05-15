"""Optional bridge to mgimo-foreign_trade's reference data + finalisers.

Looks for ``mgimo-foreign_trade/src`` next to this project (or via the
``MGIMO_FOREIGN_TRADE_SRC`` env var) and re-exports the relevant helpers so
the rest of the pipeline can ``from pipeline._mgimo import resolve_edizm_records, ...``.

If the sibling project is not present, falls back to a minimal local copy of
``COUNTRY_UNIT_ALIAS_RECORDS`` plus a trivial ``finalize_country_output`` so
``compat-export`` still works (descriptions in Russian will be unavailable).
"""
from __future__ import annotations

import sys
from typing import Any, Iterable, Optional

import pandas as pd

from .config import SETTINGS

# ---------------------------------------------------------------------------
# Hot-patch sys.path so the mgimo src tree is importable. The package layout
# under mgimo is `src/core/...` and `src/pipelines/...`, so adding `src/`
# makes both top-level packages discoverable.
# ---------------------------------------------------------------------------

_AVAILABLE = False
if SETTINGS.mgimo_src is not None and SETTINGS.mgimo_src.is_dir():
    _src = str(SETTINGS.mgimo_src)
    if _src not in sys.path:
        sys.path.insert(0, _src)
    try:
        from core.normalization_rules import (  # type: ignore  # noqa: F401
            COUNTRY_UNIT_ALIAS_RECORDS,
            normalize_edizm_value,
            resolve_edizm_record,
            resolve_edizm_records,
        )
        from core.country_processor_contract import (  # type: ignore  # noqa: F401
            COUNTRY_OUTPUT_COLUMNS,
            NAPR_NORMALIZATION,
            finalize_country_output,
            normalize_napr_value,
        )

        try:  # tnved.csv loader is project-rooted; tolerate failure
            from pipelines.merge_pipeline import (  # type: ignore  # noqa: F401
                load_common_edizm_mapping,
                load_tnved_mapping,
            )
        except Exception:  # pragma: no cover - optional
            load_common_edizm_mapping = None
            load_tnved_mapping = None

        _AVAILABLE = True
    except Exception:
        _AVAILABLE = False


# ---------------------------------------------------------------------------
# Local fallback (kept tiny, only what compat-export strictly needs).
# ---------------------------------------------------------------------------

if not _AVAILABLE:
    COUNTRY_OUTPUT_COLUMNS = (  # type: ignore[assignment]
        "NAPR", "PERIOD", "STRANA", "TNVED",
        "EDIZM", "EDIZM_ISO", "STOIM", "NETTO", "KOL",
        "TNVED4", "TNVED6", "TNVED2",
    )
    NAPR_NORMALIZATION = {  # type: ignore[assignment]
        "M": "ЭК", "X": "ИМ",
        "IMPORT": "ЭК", "EXPORT": "ИМ",
        "1": "ИМ", "2": "ЭК",
        "ИМ": "ИМ", "ЭК": "ЭК",
    }
    KG = {"KOD": "166", "NAME": "КИЛОГРАММ"}
    COUNTRY_UNIT_ALIAS_RECORDS = {  # type: ignore[assignment]
        "KG": KG, "KGS": KG, "KILOGRAM": KG,
        "ADET": {"KOD": "796", "NAME": "ШТУКА"},
        "KG/ADET": {"KOD": "796", "NAME": "ШТУКА"},
        "ÇİFT": {"KOD": "715", "NAME": "ПАРА"},
        "KG/ÇİFT": {"KOD": "715", "NAME": "ПАРА"},
        "M2": {"KOD": "055", "NAME": "КВАДРАТНЫЙ МЕТР"},
        "KG/M2": {"KOD": "055", "NAME": "КВАДРАТНЫЙ МЕТР"},
        "M3": {"KOD": "113", "NAME": "КУБИЧЕСКИЙ МЕТР"},
        "KG/M3": {"KOD": "113", "NAME": "КУБИЧЕСКИЙ МЕТР"},
        "METRE": {"KOD": "006", "NAME": "МЕТР"},
        "LİTRE": {"KOD": "112", "NAME": "ЛИТР"},
        "KG/LİTRE": {"KOD": "112", "NAME": "ЛИТР"},
    }
    load_common_edizm_mapping = None
    load_tnved_mapping = None

    def normalize_edizm_value(value: object) -> str:
        s = str(value).upper().strip()
        return s.replace("³", "3").replace("²", "2")

    def resolve_edizm_record(value, common_edizm_map=None):
        return COUNTRY_UNIT_ALIAS_RECORDS.get(normalize_edizm_value(value))

    def resolve_edizm_records(values: pd.Series, common_edizm_map=None) -> pd.Series:
        return values.apply(lambda v: resolve_edizm_record(v, common_edizm_map))

    def normalize_napr_value(value: object) -> object:
        if pd.isna(value):
            return value
        return NAPR_NORMALIZATION.get(str(value).strip().upper(), value)

    def finalize_country_output(
        df: pd.DataFrame,
        *,
        country_code: Optional[str] = None,
        sort_by: Iterable[str] = ("PERIOD", "NAPR", "TNVED"),
        drop_duplicates: bool = True,
    ) -> pd.DataFrame:
        out = df.copy()
        for col in COUNTRY_OUTPUT_COLUMNS:
            if col not in out.columns:
                out[col] = None
        out["NAPR"] = out["NAPR"].map(normalize_napr_value)
        out["PERIOD"] = pd.to_datetime(out["PERIOD"], errors="coerce").dt.normalize()
        if country_code is not None:
            out["STRANA"] = country_code
        out["STRANA"] = out["STRANA"].astype(str).str.upper()
        out["TNVED"] = out["TNVED"].astype(str).str.strip()
        for col, n in (("TNVED2", 2), ("TNVED4", 4), ("TNVED6", 6)):
            out[col] = out["TNVED"].str[:n]
        for col in ("STOIM", "NETTO", "KOL"):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        out = out[list(COUNTRY_OUTPUT_COLUMNS)]
        if drop_duplicates:
            out = out.drop_duplicates()
        cols = [c for c in sort_by if c in out.columns]
        if cols:
            out = out.sort_values(by=cols)
        return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Loaders for reference tables that live in the mgimo project root (one level
# up from `src/`). The mgimo helpers want a "project_root" argument.
# ---------------------------------------------------------------------------


def mgimo_root() -> Optional["object"]:
    if SETTINGS.mgimo_src is None:
        return None
    return SETTINGS.mgimo_src.parent


def common_edizm_mapping() -> dict[str, dict[str, str]]:
    """Return canonical EDIZM map from mgimo's metadata/edizm.csv if available."""
    if load_common_edizm_mapping is None or mgimo_root() is None:
        return {}
    try:
        return load_common_edizm_mapping(mgimo_root())
    except Exception:
        return {}


def tnved_mapping() -> dict[str, dict[str, dict[str, Any]]]:
    """Return mgimo's TNVED-to-Russian-name map if available.

    Returns {} when the sibling project (or its metadata) isn't present.
    """
    if load_tnved_mapping is None or mgimo_root() is None:
        return {}
    try:
        return load_tnved_mapping(mgimo_root())
    except Exception:
        return {}


def mgimo_available() -> bool:
    return _AVAILABLE


__all__ = [
    "COUNTRY_OUTPUT_COLUMNS",
    "COUNTRY_UNIT_ALIAS_RECORDS",
    "NAPR_NORMALIZATION",
    "common_edizm_mapping",
    "finalize_country_output",
    "mgimo_available",
    "mgimo_root",
    "normalize_edizm_value",
    "normalize_napr_value",
    "resolve_edizm_record",
    "resolve_edizm_records",
    "tnved_mapping",
]
