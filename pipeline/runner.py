"""Orchestrate TUIK BI bulk fetches across (partner, year, month)."""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterable
from datetime import date

from loguru import logger
from tqdm import tqdm

from .config import (
    DEFAULT_DIMS_GTIP12,
    DEFAULT_MEASURES,
    RUSSIA_ULKE_KODU,
    SETTINGS,
    TRADE_SYSTEM,
)
from .normalize import cube_to_dataframe
from .storage import partition_exists, write_partition
from .tuik_bi import TuikBI


def _months(year: int) -> list[int]:
    today = date.today()
    if year == today.year:
        return list(range(1, today.month + 1))  # only completed/closing months
    return list(range(1, 13))


async def fetch_month_gtip12(
    client: TuikBI,
    *,
    year: int,
    month: int,
    partner_kodu: str,
) -> int:
    """Fetch one (partner, year, month) at GTIP-12 detail; write parquet."""
    selections = {
        "ULKE_KODU": [partner_kodu],
        "YIL": [year],
        "AY": [month],
    }
    cube = await client.query(
        selections=selections,
        dims=DEFAULT_DIMS_GTIP12,
        measures=DEFAULT_MEASURES,
    )
    df = cube_to_dataframe(cube, DEFAULT_DIMS_GTIP12, DEFAULT_MEASURES)
    df["partner_kodu"] = partner_kodu
    df["trade_system"] = TRADE_SYSTEM

    write_partition(df, partner_kodu, year, month)
    return len(df)


async def run_gtip12(
    *,
    year_from: int,
    year_to: int,
    partner_kodu: str = RUSSIA_ULKE_KODU,
    app_key: str = "general_en",
    skip_existing: bool = True,
    headless: bool | None = None,
    jobs: Iterable[tuple[int, int]] | None = None,
) -> dict[tuple[int, int], int]:
    """Bulk-fetch GTIP-12 monthly data for `partner_kodu`.

    If ``jobs`` is provided, fetch exactly those ``(year, month)`` pairs.
    Otherwise enumerate ``[year_from, year_to]`` (closed) and trim to closed
    months for the current calendar year.
    """
    if jobs is None:
        jobs = [
            (y, m) for y in range(year_from, year_to + 1) for m in _months(y)
        ]
    jobs = list(jobs)
    if skip_existing:
        jobs = [
            (y, m)
            for y, m in jobs
            if not partition_exists(partner_kodu, y, m)
        ]
    if not jobs:
        logger.info("nothing to do (all partitions already present)")
        return {}

    logger.info(
        f"plan: partner={partner_kodu} app={app_key} "
        f"jobs={len(jobs)} ({jobs[0]} .. {jobs[-1]})"
    )

    counts: dict[tuple[int, int], int] = {}
    async with TuikBI(app_key=app_key, headless=headless) as client:
        for y, m in tqdm(jobs, desc=f"partner={partner_kodu}"):
            try:
                n = await fetch_month_gtip12(
                    client, year=y, month=m, partner_kodu=partner_kodu
                )
                counts[(y, m)] = n
            except Exception as e:
                logger.exception(f"failed {y}-{m:02d}: {e}")
                counts[(y, m)] = -1
    return counts


async def run_update(
    *,
    partner_kodu: str = RUSSIA_ULKE_KODU,
    app_key: str = "general_en",
    headless: bool | None = None,
    refetch_latest_n: int = 1,
) -> dict[tuple[int, int], int]:
    """Discover TUIK BI's currently-available history and fetch what's missing.

    1. Connect to TUIK BI, ask the cube which (year, month) tuples it exposes.
    2. Compare to what's on disk under ``data/raw/tuik_bi/partner_kodu=K/``.
    3. Re-download:
       * every month that doesn't exist locally yet, AND
       * the last ``refetch_latest_n`` *published* months (because TUIK revises
         them for several weeks after release — typically corrections to
         confidential-aggregation lines and late filings).
    """
    from .tuik_bi import TuikBI, discover_coverage

    async with TuikBI(app_key=app_key, headless=headless) as client:
        cov = await discover_coverage(client, partner_kodu=partner_kodu)
        if cov["latest_year"] is None:
            logger.warning("partner_kodu={} has no data in TUIK BI", partner_kodu)
            return {}

        # Enumerate every (year, month) TUIK currently exposes.
        all_jobs: list[tuple[int, int]] = []
        for y in cov["years"]:
            if y == cov["latest_year"]:
                months = cov["latest_months"]
            else:
                months = list(range(1, 13))
            all_jobs.extend((y, m) for m in months)
        all_jobs.sort()

        logger.info(
            "TUIK BI exposes {} months ({:04d}-{:02d} .. {:04d}-{:02d}); latest published: {:04d}-{:02d}",
            len(all_jobs), all_jobs[0][0], all_jobs[0][1],
            all_jobs[-1][0], all_jobs[-1][1],
            cov["latest_year"], cov["latest_month"],
        )

        # Decide what to (re)fetch.
        latest_n_set = set(all_jobs[-refetch_latest_n:]) if refetch_latest_n > 0 else set()
        missing = [j for j in all_jobs if not partition_exists(partner_kodu, *j)]
        plan = sorted(set(missing) | latest_n_set)
        if not plan:
            logger.info("up to date — nothing to fetch")
            return {}

        logger.info(
            "fetching {} months ({} missing + {} refresh of recent)",
            len(plan), len(missing), len(latest_n_set & set(all_jobs)),
        )

        counts: dict[tuple[int, int], int] = {}
        for y, m in tqdm(plan, desc=f"update partner={partner_kodu}"):
            try:
                n = await fetch_month_gtip12(
                    client, year=y, month=m, partner_kodu=partner_kodu
                )
                counts[(y, m)] = n
            except Exception as e:
                logger.exception(f"failed {y}-{m:02d}: {e}")
                counts[(y, m)] = -1
        return counts


def configure_logging(level: str | None = None) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=(level or SETTINGS.log_level).upper(),
        format="<green>{time:HH:mm:ss}</green> <level>{level: <7}</level> {message}",
    )
    SETTINGS.ensure_dirs()
    logger.add(
        SETTINGS.data_dir / "pipeline.log",
        rotation="20 MB",
        retention=5,
        level="DEBUG",
        encoding="utf-8",
    )


def main_sync(
    year_from: int,
    year_to: int,
    partner_kodu: str = RUSSIA_ULKE_KODU,
    app_key: str = "general_en",
    skip_existing: bool = True,
    headless: bool | None = None,
) -> dict[tuple[int, int], int]:
    return asyncio.run(
        run_gtip12(
            year_from=year_from,
            year_to=year_to,
            partner_kodu=partner_kodu,
            app_key=app_key,
            skip_existing=skip_existing,
            headless=headless,
        )
    )


def update_sync(
    partner_kodu: str = RUSSIA_ULKE_KODU,
    app_key: str = "general_en",
    headless: bool | None = None,
    refetch_latest_n: int = 1,
) -> dict[tuple[int, int], int]:
    return asyncio.run(
        run_update(
            partner_kodu=partner_kodu,
            app_key=app_key,
            headless=headless,
            refetch_latest_n=refetch_latest_n,
        )
    )
