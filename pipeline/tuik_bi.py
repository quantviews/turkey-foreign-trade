"""Playwright-driven client for TUIK BI Qlik Sense apps.

We open the mashup, wait for Qlik Capability API, open the chosen app,
and execute hypercube queries via the engine API directly from JS.
"""
from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from loguru import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from .config import MASHUP_URL_TPL, SETTINGS, TUIK_APPS

# ---------------------------------------------------------------------------
# JS payloads. Kept as module-level constants so they are easy to inspect.
# ---------------------------------------------------------------------------

_JS_BOOT = r"""
async (appId) => {
  await window.qlikMashupLoader.promise;
  if (!window.__q) {
    window.__q = await new Promise((res, rej) => window.require(['qlik'], res, rej));
  }
  window.__app = window.__q.openApp(appId, {
    host: location.hostname,
    prefix: '/',
    port: location.port || 443,
    isSecure: true,
  });
  await new Promise((r) => setTimeout(r, 3500));
  // sanity: list a few fields
  const fl = await new Promise((res) => {
    window.__app.getList('FieldList', (reply) => res(reply?.qFieldList?.qItems?.length || 0));
    setTimeout(() => res(-1), 10000);
  });
  return { ok: true, fields: fl };
}
"""

# Run a hypercube query. JSON-serialised args: selections, dims, measures.
# We translate `selections` -> Qlik set-analysis tokens that are merged into
# every measure expression. This avoids any global selection state and is
# idempotent. Qlik also has a ~10k cell-per-fetch ceiling, so we paginate.
_JS_QUERY = r"""
async (args) => {
  const { setAnalysis, dims, measures } = args;
  const app = window.__app;
  const engine = app.model.engineApp;

  await app.clearAll(false);

  const qDimensions = dims.map((d) => ({
    qDef: { qFieldDefs: [d] },
    qNullSuppression: true,
  }));
  // Wrap each measure's expression in the same set-analysis filter.
  const wrap = (expr) => setAnalysis
    ? expr.replace(/^Sum\(/i, `Sum(${setAnalysis} `)
    : expr;
  const qMeasures = measures.map((m) => ({
    qDef: {
      qDef: wrap(m.expr),
      qLabel: m.label,
      qNumFormat: { qType: 'F', qFmt: '#0.0000' },
    },
  }));
  const width = dims.length + measures.length;
  const CELL_CAP = 8000;
  const pageHeight = Math.max(50, Math.floor(CELL_CAP / Math.max(1, width)));

  const obj = await engine.createSessionObject({
    qInfo: { qType: 'mycube' },
    qHyperCubeDef: {
      qDimensions,
      qMeasures,
      qInitialDataFetch: [
        { qLeft: 0, qTop: 0, qWidth: width, qHeight: pageHeight },
      ],
      qSuppressZero: false,
      qSuppressMissing: false,
    },
  });

  const layout = await obj.getLayout();
  const hc = layout.qHyperCube;
  const totalRows = hc.qSize.qcy;

  let matrix = (hc.qDataPages[0] || {}).qMatrix || [];
  let top = matrix.length;
  let safety = 0;
  while (top < totalRows && safety < 5000) {
    safety++;
    const next = await obj.getHyperCubeData('/qHyperCubeDef', [
      { qLeft: 0, qTop: top, qWidth: width, qHeight: pageHeight },
    ]);
    const more = next?.[0]?.qMatrix || [];
    if (!more.length) break;
    matrix = matrix.concat(more);
    top += more.length;
  }

  return {
    totalRows,
    rows: matrix.length,
    pageHeight,
    dimInfo: hc.qDimensionInfo.map((d) => d.qFallbackTitle),
    measInfo: hc.qMeasureInfo.map((m) => m.qFallbackTitle),
    matrix: matrix.map((row) =>
      row.map((c) => ({ t: c.qText, n: typeof c.qNum === 'number' ? c.qNum : null }))
    ),
  };
}
"""


# ---------------------------------------------------------------------------
# Public client
# ---------------------------------------------------------------------------


class TuikBI:
    """Async context manager wrapping a Playwright browser + Qlik mashup."""

    def __init__(
        self,
        app_key: str = "general_en",
        headless: bool | None = None,
        slow_mo_ms: int = 0,
    ):
        if app_key not in TUIK_APPS:
            raise ValueError(f"unknown app_key {app_key!r}; expected one of {list(TUIK_APPS)}")
        self.app_key = app_key
        self.app_id = TUIK_APPS[app_key]
        self.lang = "en" if app_key.endswith("_en") else "tr"
        self.headless = SETTINGS.headless if headless is None else headless
        self.slow_mo_ms = slow_mo_ms

        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None

    async def __aenter__(self) -> "TuikBI":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo_ms,
        )
        self._ctx = await self._browser.new_context(
            user_agent=SETTINGS.user_agent,
            viewport={"width": 1600, "height": 1000},
        )
        self._page = await self._ctx.new_page()
        self._page.on(
            "console",
            lambda m: logger.debug(f"[browser.{m.type}] {m.text[:300]}"),
        )

        url = MASHUP_URL_TPL.format(lang=self.lang)
        logger.info(f"opening mashup {url} (app={self.app_key} id={self.app_id})")
        await self._page.goto(url, wait_until="networkidle", timeout=60_000)

        # Bootstrap Qlik + open app
        result = await self._page.evaluate(_JS_BOOT, self.app_id)
        if not result.get("ok") or result.get("fields", 0) <= 0:
            raise RuntimeError(f"failed to bootstrap Qlik app: {result}")
        logger.info(f"Qlik ready, fields={result['fields']}")
        return self

    async def __aexit__(self, *exc) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()

    @staticmethod
    def _build_set_analysis(selections: dict[str, Iterable[str | int]]) -> str:
        """Render a Qlik set-analysis prefix from a {field: [values]} dict.

        Example: {"YIL": [2024], "ULKE_KODU": ["75"]} ->
            "{<YIL={'2024'}, ULKE_KODU={'75'}>}"

        Empty selections => "" (no filter).
        """
        if not selections:
            return ""
        parts: list[str] = []
        for field, values in selections.items():
            vals = [str(v) for v in values]
            if not vals:
                continue
            quoted = ",".join(f"'{v}'" for v in vals)
            parts.append(f"{field}={{{quoted}}}")
        return "{<" + ", ".join(parts) + ">}" if parts else ""

    async def query(
        self,
        selections: dict[str, Iterable[str | int]],
        dims: list[str],
        measures: list[dict[str, str]],
        page_height: int | None = None,  # auto-sized in JS
    ) -> dict[str, Any]:
        """Run a single hypercube query with set-analysis selections."""
        if self._page is None:
            raise RuntimeError("client not entered")
        set_expr = self._build_set_analysis(selections)
        args = {
            "setAnalysis": set_expr,
            "dims": dims,
            "measures": measures,
        }
        t0 = time.monotonic()
        out = await self._page.evaluate(_JS_QUERY, args)
        dt = time.monotonic() - t0
        logger.debug(
            f"query rows={out['rows']}/{out['totalRows']} "
            f"page={out.get('pageHeight')} dims={dims} set={set_expr} t={dt:.1f}s"
        )
        if out["rows"] < out["totalRows"]:
            logger.warning(
                f"truncated: returned {out['rows']} of {out['totalRows']} rows"
            )
        return out


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------


async def list_countries(client: TuikBI) -> list[dict[str, str]]:
    """Return [{ulke_kodu, ulke_adi}] for all countries in the cube."""
    out = await client.query(
        selections={},
        dims=["ULKE_KODU", "ULKE_ADI"],
        measures=[],
        page_height=5000,
    )
    return [
        {"ulke_kodu": row[0]["t"], "ulke_adi": row[1]["t"]}
        for row in out["matrix"]
    ]


async def discover_field_values(client: TuikBI, field: str, limit: int = 1000) -> list[str]:
    out = await client.query(
        selections={},
        dims=[field],
        measures=[],
        page_height=limit,
    )
    return [row[0]["t"] for row in out["matrix"]]


async def discover_coverage(
    client: TuikBI,
    partner_kodu: str | None = None,
) -> dict[str, Any]:
    """Return what (years, months) TUIK BI currently exposes.

    If ``partner_kodu`` is given, scope to that partner only; otherwise return
    the global cube coverage.

    Result shape::

        {
          "years": [2013, ..., 2026],
          "latest_year": 2026,
          "latest_months": [1, 2, 3],
          "latest_month": 3,
        }
    """
    selections: dict[str, list[str | int]] = {}
    if partner_kodu is not None:
        selections["ULKE_KODU"] = [partner_kodu]

    one_meas = [{"id": "n", "expr": "Sum(1)", "label": "n"}]
    out_y = await client.query(selections=selections, dims=["YIL"], measures=one_meas)
    years = sorted({int(row[0]["t"]) for row in out_y["matrix"] if row[0]["t"]})
    if not years:
        return {"years": [], "latest_year": None, "latest_months": [], "latest_month": None}

    latest_year = years[-1]
    out_m = await client.query(
        selections={**selections, "YIL": [latest_year]},
        dims=["AY"],
        measures=one_meas,
    )
    months = sorted({int(row[0]["t"]) for row in out_m["matrix"] if row[0]["t"]})
    return {
        "years": years,
        "latest_year": latest_year,
        "latest_months": months,
        "latest_month": months[-1] if months else None,
    }
