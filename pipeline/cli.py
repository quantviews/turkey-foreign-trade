"""Typer CLI."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import typer
from loguru import logger

from .comtrade import fetch_range as comtrade_range
from .config import RUSSIA_ULKE_KODU, SETTINGS, TRADE_SYSTEM
from . import queries
from .runner import configure_logging, main_sync, update_sync
from .storage import register_duckdb

app = typer.Typer(add_completion=False, help="Turkey foreign-trade pipeline (Russia by default).")


@app.callback()
def _root(
    log_level: str = typer.Option(SETTINGS.log_level, "--log-level", "-v"),
) -> None:
    configure_logging(log_level)


@app.command()
def tuik(
    year_from: int = typer.Option(date.today().year - 9, "--from", help="First year inclusive."),
    year_to: int = typer.Option(date.today().year, "--to", help="Last year inclusive."),
    partner_kodu: str = typer.Option(RUSSIA_ULKE_KODU, "--partner", help="TUIK ULKE_KODU (Russia=75)."),
    lang: str = typer.Option(
        "en", "--lang",
        help="Qlik app language: en|tr (GTS only — STS is not supported).",
    ),
    skip_existing: bool = typer.Option(True, "--skip-existing/--rerun"),
    headed: bool = typer.Option(False, "--headed", help="Show the browser window."),
) -> None:
    """Fetch GTIP-12 monthly trade for a partner from TUIK BI mashup (General Trade System)."""
    app_key = f"{TRADE_SYSTEM}_{lang}"
    main_sync(
        year_from=year_from,
        year_to=year_to,
        partner_kodu=partner_kodu,
        app_key=app_key,
        skip_existing=skip_existing,
        headless=not headed,
    )


@app.command()
def update(
    partner_kodu: str = typer.Option(RUSSIA_ULKE_KODU, "--partner", help="TUIK ULKE_KODU."),
    lang: str = typer.Option("en", "--lang", help="Qlik app language: en|tr."),
    headed: bool = typer.Option(False, "--headed", help="Show the browser."),
    refresh_latest: int = typer.Option(
        1, "--refresh-latest",
        help="Re-download N most recent months (TUIK revises them for ~weeks). 0 = no refresh.",
    ),
) -> None:
    """Discover what TUIK BI currently publishes and fetch every missing month.

    Use this for routine updates after the initial bulk pull. It auto-detects
    the latest published month and refreshes the most recent ones because TUIK
    revises late filings / confidential aggregations for several weeks.
    """
    app_key = f"{TRADE_SYSTEM}_{lang}"
    counts = update_sync(
        partner_kodu=partner_kodu,
        app_key=app_key,
        headless=not headed,
        refetch_latest_n=refresh_latest,
    )
    if counts:
        typer.echo(f"updated {len(counts)} month(s)")
        for (y, m), n in sorted(counts.items()):
            typer.echo(f"  {y}-{m:02d}: {n:>7,} rows")
    else:
        typer.echo("nothing to update")


@app.command()
def coverage(
    partner_kodu: str = typer.Option(RUSSIA_ULKE_KODU, "--partner"),
    lang: str = typer.Option("en", "--lang"),
) -> None:
    """Print what TUIK BI exposes vs what's on disk (drift between cloud + local)."""
    import asyncio
    from .tuik_bi import TuikBI, discover_coverage
    from .storage import partition_exists

    app_key = f"{TRADE_SYSTEM}_{lang}"

    async def _run() -> dict:
        async with TuikBI(app_key=app_key) as c:
            return await discover_coverage(c, partner_kodu=partner_kodu)

    cov = asyncio.run(_run())
    if not cov["years"]:
        typer.echo(f"partner={partner_kodu}: nothing in TUIK BI"); return

    typer.echo(f"TUIK BI for partner={partner_kodu}:")
    typer.echo(f"  years: {cov['years'][0]}..{cov['years'][-1]} ({len(cov['years'])} years)")
    typer.echo(f"  latest published: {cov['latest_year']}-{cov['latest_month']:02d}")
    typer.echo(f"  latest year months: {cov['latest_months']}")

    # Compare with local
    expected: list[tuple[int, int]] = []
    for y in cov["years"]:
        months = cov["latest_months"] if y == cov["latest_year"] else list(range(1, 13))
        expected.extend((y, m) for m in months)
    missing = [j for j in expected if not partition_exists(partner_kodu, *j)]
    present = len(expected) - len(missing)
    typer.echo(f"Local: {present}/{len(expected)} months on disk")
    if missing:
        typer.echo(f"Missing: {missing}")


@app.command("compat-export")
def cmd_compat_export(
    partner_kodu: str = typer.Option(RUSSIA_ULKE_KODU, "--partner", help="TUIK ULKE_KODU."),
    year_from: int = typer.Option(None, "--from", help="First year inclusive."),
    year_to: int = typer.Option(None, "--to", help="Last year inclusive."),
    hs_level: int = typer.Option(
        8, "--hs",
        help="HS digits in TNVED column. 8=drop-in for tr_full.parquet, 12=full GTIP detail.",
    ),
    country_code: str = typer.Option("TR", "--country", help="STRANA value (ISO-2)."),
    out: Path = typer.Option(None, "--out", help="Output parquet path (default: data/exports/tr_full_compat.parquet)."),
    no_aggregate: bool = typer.Option(False, "--no-aggregate", help="Skip groupby SUM at hs<12."),
) -> None:
    """Convert TUIK GTIP-12 partitions → mgimo-compatible parquet (NAPR/PERIOD/STRANA/TNVED/...).

    Use this command to produce a drop-in replacement for the old
    ``tr_full.parquet``. Extra columns: ISTPOZ, ISTPOZ_ADI, TNVED_EN_NAME,
    TNVED_RU_NAME.
    """
    from .compat import compat_export

    path = compat_export(
        partner_kodu=partner_kodu,
        year_from=year_from,
        year_to=year_to,
        hs_level=hs_level,
        country_code=country_code,
        out=out,
        aggregate=not no_aggregate,
    )
    typer.echo(f"-> {path}")


@app.command("hs-sync")
def cmd_hs_sync(
    refresh: bool = typer.Option(True, "--refresh/--no-refresh", help="Re-download even if cached."),
) -> None:
    """Download/refresh the HS-6 EN reference table (datasets/harmonized-system)."""
    from .hs_names import load_hs6_table

    df = load_hs6_table(refresh=refresh)
    typer.echo(f"HS-6 EN reference: {len(df):,} codes (cached in {SETTINGS.refs_dir})")


@app.command("backfill-trade-system")
def cmd_backfill_trade_system() -> None:
    """One-shot: add trade_system='general' column to existing parquets in place."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    paths = sorted((SETTINGS.raw_dir / "tuik_bi").rglob("*.parquet"))
    n = 0
    for p in paths:
        pf = pq.ParquetFile(p)
        table = pf.read()
        if "trade_system" in table.column_names:
            continue
        col = pa.array([TRADE_SYSTEM] * table.num_rows, type=pa.string())
        table = table.append_column("trade_system", col)
        pq.write_table(table, p, compression="zstd", compression_level=9)
        n += 1
        typer.echo(f"backfilled {p}")
    typer.echo(f"done: {n} files updated, {len(paths) - n} already had column")


@app.command()
def comtrade(
    year_from: int = typer.Option(date.today().year - 9, "--from"),
    year_to: int = typer.Option(date.today().year, "--to"),
) -> None:
    """Fetch UN Comtrade Turkey↔Russia HS-6 monthly data."""
    if not SETTINGS.comtrade_key:
        logger.warning(
            "UN_COMTRADE_KEY not set. Falling back to the public preview "
            "endpoint (rows capped per call). Register a free key at "
            "https://comtradedeveloper.un.org/ for full data."
        )
    comtrade_range(year_from, year_to)


@app.command()
def duckdb_view() -> None:
    """(Re)register the DuckDB view over all parquet partitions."""
    con = register_duckdb()
    rows = con.execute("SELECT COUNT(*) FROM tuik_trade").fetchone()[0]
    typer.echo(f"tuik_trade view ready: {rows:,} rows | db={SETTINGS.duckdb_path}")


@app.command()
def countries(
    contains: str = typer.Option("", help="Substring (case-insensitive) to filter."),
) -> None:
    """List all countries available in the TUIK BI cube with their ULKE_KODU."""
    import asyncio

    from .tuik_bi import TuikBI, list_countries

    async def _run() -> list[dict[str, str]]:
        async with TuikBI(app_key="general_en") as c:
            return await list_countries(c)

    rows = asyncio.run(_run())
    needle = contains.lower().strip()
    if needle:
        rows = [r for r in rows if needle in r["ulke_adi"].lower()]
    for r in rows:
        typer.echo(f"{r['ulke_kodu']:>5}  {r['ulke_adi']}")


@app.command("monthly")
def cmd_monthly(
    partner_kodu: str = typer.Option(RUSSIA_ULKE_KODU, "--partner"),
    csv: bool = typer.Option(False, "--csv", help="Also dump to data/exports/"),
) -> None:
    """Show monthly totals (USD/EUR/TRY) for a partner."""
    df = queries.monthly_totals(partner_kodu)
    typer.echo(df.to_string(index=False))
    if csv:
        path = queries.to_csv(df, f"monthly_partner={partner_kodu}.csv")
        typer.echo(f"-> {path}")


@app.command("top")
def cmd_top(
    partner_kodu: str = typer.Option(RUSSIA_ULKE_KODU, "--partner"),
    year: int = typer.Option(None, "--year"),
    flow: str = typer.Option(None, "--flow", help="X=Export, M=Import"),
    n: int = typer.Option(20, "--n"),
    hs_level: int = typer.Option(12, "--hs-level"),
    csv: bool = typer.Option(False, "--csv"),
) -> None:
    """Top N HS products by USD trade value."""
    df = queries.top_products(partner_kodu, year=year, flow=flow, n=n, hs_level=hs_level)
    typer.echo(df.to_string(index=False))
    if csv:
        suffix = f"_y{year}" if year else ""
        suffix += f"_f{flow}" if flow else ""
        path = queries.to_csv(df, f"top{n}_hs{hs_level}{suffix}.csv")
        typer.echo(f"-> {path}")


@app.command()
def smoke(
    year: int = typer.Option(date.today().year - 1, "--year"),
    month: int = typer.Option(1, "--month"),
) -> None:
    """One-month smoke test: Russia, given year/month, prints first 5 rows."""
    import asyncio

    import pandas as pd

    from .config import DEFAULT_DIMS_GTIP12, DEFAULT_MEASURES
    from .normalize import cube_to_dataframe
    from .tuik_bi import TuikBI

    async def _run() -> pd.DataFrame:
        async with TuikBI(app_key="general_en") as c:
            cube = await c.query(
                selections={
                    "ULKE_KODU": [RUSSIA_ULKE_KODU],
                    "YIL": [year],
                    "AY": [month],
                },
                dims=DEFAULT_DIMS_GTIP12,
                measures=DEFAULT_MEASURES,
            )
            return cube_to_dataframe(cube, DEFAULT_DIMS_GTIP12, DEFAULT_MEASURES)

    df = asyncio.run(_run())
    typer.echo(f"got {len(df):,} rows for Russia {year}-{month:02d}")
    typer.echo(df.head().to_string())
    typer.echo(
        df.groupby("flow")[["usd", "eur", "try"]].sum().round(0).to_string()
    )


if __name__ == "__main__":
    app()
