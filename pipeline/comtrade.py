"""Backup data source: UN Comtrade Plus (HS-6 monthly bilateral).

Free tier requires registering at https://comtradedeveloper.un.org/
and setting UN_COMTRADE_KEY in .env. Without a key, fall back to the
public preview endpoint (limited to ~500 rows per call).
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import SETTINGS

# Comtrade reporter/partner codes (M49 numeric).
REPORTER_TURKIYE = "792"
PARTNER_RUSSIA = "643"

BASE_AUTH = "https://comtradeapi.un.org/data/v1/get"
BASE_PUBLIC = "https://comtradeapi.un.org/public/v1/preview"


def _base() -> str:
    return BASE_AUTH if SETTINGS.comtrade_key else BASE_PUBLIC


def _headers() -> dict[str, str]:
    return {"Ocp-Apim-Subscription-Key": SETTINGS.comtrade_key} if SETTINGS.comtrade_key else {}


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=30), reraise=True)
def _get(url: str, params: dict) -> dict:
    r = requests.get(url, params=params, headers=_headers(), timeout=60)
    if r.status_code in (429, 503):
        raise RuntimeError(f"comtrade {r.status_code} (rate limited)")
    r.raise_for_status()
    return r.json()


def fetch_monthly(
    year: int,
    months: list[int] | None = None,
    flow: str = "M,X",
    cmd_level: str = "AG6",
    reporter: str = REPORTER_TURKIYE,
    partner: str = PARTNER_RUSSIA,
) -> pd.DataFrame:
    """Fetch Turkey<->Russia HS-6 monthly trade for given year (and optional months).

    Parameters
    ----------
    year
        Calendar year.
    months
        Subset of months 1..12; default is all.
    flow
        Comma-separated: M=import, X=export, RM=re-import, RX=re-export.
    cmd_level
        AG6 (HS6), AG4, AG2; or 'TOTAL'.
    """
    months = months or list(range(1, 13))
    period = ",".join(f"{year}{m:02d}" for m in months)
    url = f"{_base()}/C/M/HS"
    params = {
        "reporterCode": reporter,
        "partnerCode": partner,
        "period": period,
        "flowCode": flow,
        "cmdCode": cmd_level,
        "maxRecords": 100000,
        "includeDesc": "true",
    }
    logger.info(f"comtrade GET {url} year={year} months={months}")
    payload = _get(url, params)
    data = payload.get("data") or []
    if not data:
        logger.warning(f"comtrade empty response for {year}: {payload.get('error', payload)}")
    df = pd.DataFrame(data)
    if not df.empty:
        # Comtrade returns YYYYMM in 'period'; split into year/month.
        df["year"] = df["period"].astype(str).str[:4].astype(int)
        df["month"] = df["period"].astype(str).str[4:6].astype(int)
    return df


def save(df: pd.DataFrame, year: int) -> Path:
    out = SETTINGS.data_dir / "comtrade" / f"turkey_russia_{year}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, compression="zstd", index=False)
    logger.info(f"saved comtrade -> {out} ({len(df)} rows)")
    return out


def fetch_range(
    year_from: int,
    year_to: int,
    polite_sleep: float = 1.5,
) -> list[Path]:
    paths: list[Path] = []
    for y in range(year_from, year_to + 1):
        df = fetch_monthly(y)
        if not df.empty:
            paths.append(save(df, y))
        time.sleep(polite_sleep)
    return paths
