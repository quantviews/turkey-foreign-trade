"""Static configuration: Qlik app GUIDs, fields, URLs, defaults.

This project is **General Trade System (GTS) only** — per UN IMTS 2010
recommendation and to stay compatible with UN Comtrade (Turkey reports GTS to
Comtrade since ~2014) and Russian customs / Eurostat mirror data.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# We only ever use the General Trade System apps. STS apps are not exposed
# anywhere in this codebase to prevent accidental cross-system mixing.
TRADE_SYSTEM = "general"

TUIK_APPS: dict[str, str] = {
    "general_en": "6310efbf-deef-43d9-b397-dfcf355ce1fd",
    "general_tr": "bd4b4757-a3c9-45ba-b4fb-5c8d7e2d2c42",
}

MASHUP_URL_TPL = "https://bi.tuik.gov.tr/extensions/tuik-mashup/index.html?lang={lang}"

# TUIK country code for Russia (NB: TUIK uses its own coding, not ISO numeric).
RUSSIA_ULKE_KODU = "75"

# Default measure set (Qlik expressions).
DEFAULT_MEASURES: list[dict[str, str]] = [
    {"id": "usd", "expr": "Sum(DOLAR)",    "label": "USD"},
    {"id": "eur", "expr": "Sum(EURO)",     "label": "EUR"},
    {"id": "try", "expr": "Sum(TL)",       "label": "TRY"},
    {"id": "q1",  "expr": "Sum(MIKTAR_1)", "label": "Qty1"},
    {"id": "q2",  "expr": "Sum(MIKTAR_2)", "label": "Qty2"},
]

# Dimensions for the maximum-detail (GTIP-12) cube.
DEFAULT_DIMS_GTIP12: list[str] = [
    "YIL",            # year
    "AY",             # month
    "IHRITH",         # flow ("İhracat"=Export / "İthalat"=Import)
    "ISTPOZ",         # GTIP 12-digit code
    "ISTPOZ_ADI",     # GTIP 12-digit description (Turkish)
    "OLCU_ADI",       # supplementary unit name (matches MIKTAR_2)
]


def _autodetect_mgimo_src() -> Path | None:
    """Try to locate the sibling `mgimo-foreign_trade/src` directory."""
    env = os.getenv("MGIMO_FOREIGN_TRADE_SRC")
    if env:
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() else None
    here = Path(__file__).resolve().parents[1]
    for cand in (
        here.parent / "mgimo-foreign_trade" / "src",
        here.parent.parent / "mgimo-foreign_trade" / "src",
    ):
        if cand.is_dir():
            return cand.resolve()
    return None


@dataclass(frozen=True)
class Settings:
    headless: bool = os.getenv("HEADLESS", "true").lower() == "true"
    lang: str = os.getenv("TUIK_APP_LANG", "en")
    data_dir: Path = Path(os.getenv("DATA_DIR", "./data")).resolve()
    raw_dir: Path = Path(os.getenv("DATA_DIR", "./data")).resolve() / "raw"
    refs_dir: Path = Path(os.getenv("DATA_DIR", "./data")).resolve() / "refs"
    duckdb_path: Path = Path(os.getenv("DATA_DIR", "./data")).resolve() / "trade.duckdb"
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    comtrade_key: str | None = os.getenv("UN_COMTRADE_KEY") or None
    mgimo_src: Path | None = _autodetect_mgimo_src()
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    )

    def ensure_dirs(self) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.refs_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "comtrade").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "exports").mkdir(parents=True, exist_ok=True)


SETTINGS = Settings()
SETTINGS.ensure_dirs()
