# Chart Data Rendering Guide

How to build a native UI on top of the `/api/data/...` endpoints. Written for
upstream apps (and agents) that want to draw their own charts instead of using
the PNG images returned by `/api/charts/...` and the image tool routes.

Production base URL: `https://underlying-terminal-production.up.railway.app`

The data endpoints run the exact same market-data fetches and indicator math as
the rendered chart endpoints (they share the same builder code), so numbers in
your UI will match the terminal's PNGs for the same request.

---

## Endpoints

| Route | Returns |
| --- | --- |
| `POST /api/data/charts/<chart_type>` | Batch envelope with `datasets[]` (one per ticker, or per ticker+window for ridge-growth) |
| `POST /api/data/tools/torque` | Single torque dataset (score + chartable series) |
| `POST /api/data/tools/moneyline` | Single options open-interest ladder dataset |

`chart_type` is one of: `auction`, `performance`, `regression`, `ridge-growth`,
`flow-compass`, `torque`, `portfolio`, `volatility`. Underscores are accepted
(`ridge_growth` → `ridge-growth`).

Request bodies are identical to the image routes. Ticker selection (all routes):

| Field | Type | Notes |
| --- | --- | --- |
| `ticker` | string | Single symbol |
| `tickers` | string or string[] | Comma-separated string or array |
| `watchlist_url` | string | Public TradingView watchlist URL (takes precedence) |
| `max_results` | int | Default 10, cap 50 |

Per-type extras:

Supported `period` tokens: `5d` (≈ one trading week), `1mo`, `3mo`, `6mo`,
`1y`, `2y`, `5y`, `10y`.

| Type | Extra body fields | History window used |
| --- | --- | --- |
| `auction` | `period` (default `1y`) | `period` |
| `performance` | `month` 1–12 (default 1) | fixed 10y |
| `regression` | `period` (default `1y`), `start_date`, `end_date` | `period` or explicit range |
| `ridge-growth` | none | fixed windows: 6mo, 1y, 2y (3 datasets per ticker) |
| `flow-compass` | `period` (default `1y`) | `period`, daily bars |
| `torque` | `period` (default `2y`) | `period`, daily bars |
| `portfolio` | `investment_per_stock` (default 100), `benchmark_ticker` (default SPY), `start_date`, `end_date` | 1y or explicit range |
| `volatility` | none | fixed 1y |

Errors: per-ticker failures land in `meta.errors` (`[{ticker, error}]`) and the
call still succeeds with the tickers that worked. If every ticker fails the
route returns `400 {"error": "..."}`.

---

## Response envelope (`/api/data/charts/<type>`)

```json
{
  "datasets": [ { "chart_type": "...", "ticker": "...", "meta": {}, "series": {} } ],
  "provider": "yfinance",
  "provider_note": "Batch auction chart data",
  "meta": {
    "result_count": 1,
    "error_count": 0,
    "watchlist_name": "Manual tickers",
    "results": [ { "ticker": "AAPL", "provider": "yfinance", "provider_note": "...", "meta": {} } ],
    "errors": []
  },
  "watchlist": null,
  "export": { "mode": "auction-data", "tickers": ["AAPL"], "image_files": [], "datasets": [] }
}
```

- `datasets[]` is the payload you render. One entry per ticker; ridge-growth
  emits one per ticker **per window** (6mo/1y/2y).
- When exactly one result exists, that dataset's `meta` is also merged into the
  top-level `meta` (legacy convenience; prefer reading `datasets[n].meta`).
- There is never an `images` key on data routes; `export.image_files` is `[]`.
- The two `/api/data/tools/...` routes return the dataset at the top level
  (plus an `export`), not wrapped in `datasets[]`.

## Point shapes

All time series use one of these row shapes:

```json
{ "date": "2026-08-14", "value": 231.59 }                      // value series
{ "date": "2026-08-14", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1 }  // ohlcv
{ "date": "2026-08-14", "Close": 1.0, "buy_signal": false, "state": "NEUTRAL" }     // signal frames: typed columns, null when missing
```

Dates are ISO `YYYY-MM-DD`, trading days only (no weekend gap-filling — plot on
a category/time axis that skips missing days, exactly like the terminal does).

---

## Dataset schemas by chart type

### auction

```json
{
  "chart_type": "auction", "ticker": "AAPL", "period": "1y",
  "meta":   { "vah": 1, "val": 1, "poc": 1, "location": "inside value", "distance_to_poc": 0.01 },
  "levels": { "vah": 1, "val": 1, "poc": 1 },
  "series": { "ohlcv": [], "close": [] }
}
```

Semantics: levels come from the trailing 21 completed sessions (VAH = max High,
VAL = min Low, POC = median Close). `location` is one of `above value`,
`inside value`, `below value`.

### performance

```json
{
  "chart_type": "performance", "ticker": "AAPL",
  "meta": { "selected_month": "Aug", "mean_5y": 1.9 },
  "table": {
    "columns": ["2017", "...", "2026", "Mean 5Y", "Median 5Y"],
    "rows": [ { "month": 8, "month_label": "Aug", "values": { "2026": 3.1, "Mean 5Y": 1.9 } } ]
  }
}
```

A monthly-return (%) heatmap grid: 12 month rows (rotated so the requested
`month` is first) x 10 year columns plus `Mean 5Y` / `Median 5Y`. `null` cell =
no data for that month/year.

### regression

```json
{
  "chart_type": "regression", "ticker": "AAPL",
  "meta": { "slope_per_day": 0.12, "residual_std": 4.2, "intercept": 180.0 },
  "series": {
    "ohlcv": [], "close": [], "trend": [], "upper_band": [], "lower_band": [],
    "ema21": [], "ema50": [], "ema200": [], "volume": []
  }
}
```

`trend` is the linear regression of close over the window; bands are trend
±1 residual sigma.

### ridge-growth (one dataset per window: 6mo, 1y, 2y)

```json
{
  "chart_type": "ridge-growth", "ticker": "AAPL", "period": "1y",
  "meta": {
    "state": "LONG|WATCH|CASH", "recommendation": "BUY|SELL|HOLD LONG|BUY SETUP|WATCH|CASH",
    "ending_equity": 1, "total_return": 0.2, "max_drawdown": -0.1,
    "closed_trades": 3, "win_rate": 0.66, "buy_count": 4, "sell_count": 3,
    "open_position_qty": 10, "open_position_return": 0.05, "latest_close": 1,
    "fast_ema": 1, "base_ema": 1, "major_sma": 1, "trend_confirmed": true,
    "cash_to_use": 1, "shares_to_buy": 1, "exit_style": "Major Breakdown",
    "trades": [ { "entry_date": "", "exit_date": "", "quantity": 1, "entry_price": 1, "exit_price": 1, "pnl": 1, "return": 0.1 } ],
    "equity_curve": [],
    "flow_compass": { "state": "", "score": 1, "signal": 1 },
    "auction": { "vah": 1, "val": 1, "poc": 1, "location": "" },
    "analysis_memo": "### AAPL Ridge + Flow Read ... (markdown, last window only)"
  },
  "series": {
    "ohlcv": [], "close": [], "fast_ma": [], "base_ma": [], "major_ma": [], "equity": [],
    "signals": [ { "date": "", "Close": 1, "Low": 1, "High": 1, "in_trade": true,
                    "buy_signal": false, "sell_signal": false, "trend_on": true,
                    "trend_confirmed": true, "rsi_14": 55.0 } ]
  }
}
```

Strategy simulation starts at $10,000. `fast_ma`/`base_ma` are EMA 75/150,
`major_ma` is SMA 200.

### flow-compass

```json
{
  "chart_type": "flow-compass", "ticker": "AAPL", "period": "1y",
  "meta": { "state": "NEUTRAL", "score": 12.0, "signal": 9.0,
            "volume_score": 1, "trend_score": 1, "momentum_score": 1,
            "value_score": 1, "rvi_score": 1, "fresh_long": false, "fresh_short": false,
            "trigger_level": 25.0, "strong_level": 55.0 },
  "levels": { "trigger_level": 25.0, "strong_level": 55.0 },
  "series": {
    "ohlcv": [], "close": [], "flow_score": [], "compass_signal": [],
    "signals": [ { "date": "", "flow_score": 1, "compass_signal": 1, "fresh_long": false,
                    "fresh_short": false, "long_ok": false, "short_ok": false, "state": "NEUTRAL",
                    "volume_score": 1, "trend_score": 1, "momentum_score": 1,
                    "value_score": 1, "rvi_score": 1, "Close": 1, "Low": 1, "High": 1 } ]
  }
}
```

Scores are clamped to ±100. States: `STRONG LONG`, `LONG OK`, `STRONG SHORT`,
`AVOID CALLS`, `NEUTRAL`.

### torque (also `POST /api/data/tools/torque`, single-ticker)

```json
{
  "chart_type": "torque", "ticker": "AAPL",
  "meta": {
    "total_score": 62.1, "stage_label": "Inflecting", "stage_detail": "...",
    "recommendation": "BUY SETUP", "target_zone": "scale in",
    "components": { "Revenue Inflection": { "score": 70, "weight": 0.25, "detail": "..." } },
    "weights": {}, "fundamental_data_available": true
  },
  "torque": { "total_score": 1, "stage_label": "", "stage_detail": "", "recommendation": "",
               "components": [ { "name": "", "score": 1, "weight": 0.25, "detail": "" } ],
               "target_zone": "" },
  "series": {
    "price": { "close": [], "ema75": [], "sma200": [], "sma50": [], "ohlcv": [] },
    "fundamentals": {
      "revenue":          [ { "label": "FY26Q2", "value": 1 } ],
      "gross_margin":     [ { "label": "FY26Q2", "value": 42.1 } ],
      "operating_margin": [ { "label": "FY26Q2", "value": 18.3 } ]
    }
  }
}
```

`series.price` is empty (`{}`) when no price history exists. Key fundamentals
panels off the arrays themselves: they can be empty even when
`meta.fundamental_data_available` is `true` — the flag means SEC trend data
informed the score, not that quarterly series exist (some issuers report YoY
scalars without quarterly breakdowns). Margins are percentages (0–100). Stages:
`Coiled Spring`, `Inflecting`, `Proof Phase`, `Renaming Phase`, `Extended`,
`No Setup`.

Note: `POST /api/data/tools/torque` always uses a 2y daily window and ignores
`period`; the `period` extra applies only to `POST /api/data/charts/torque`.

### portfolio (one dataset for the whole basket)

```json
{
  "chart_type": "portfolio", "tickers": ["AAPL", "MSFT"],
  "meta": { "final_values": {"AAPL": 1}, "initial_value": 1, "portfolio_final": 1,
            "total_return": 0.1, "max_drawdown": -0.05, "annualized_volatility": 0.2,
            "investment_per_stock": 100,
            "benchmark_ticker": "SPY", "benchmark_return": 0.1, "alpha_vs_benchmark": 0.0,
            "benchmark_final": 1, "benchmark_equity_curve": [] },
  "series": { "portfolio": [], "holdings": { "AAPL": [], "MSFT": [] }, "benchmark": [] }
}
```

Every holding is normalized to `investment_per_stock` dollars at the first bar;
`portfolio` is the sum. Benchmark keys only exist when the benchmark resolved.

### volatility (one dataset for the whole list)

```json
{
  "chart_type": "volatility", "tickers": ["AAPL", "MSFT"],
  "rows": [ { "ticker": "AAPL", "price": 1, "daily_vol": 0.012, "annual_vol": 0.19,
               "one_week_range": 5.2, "one_month_range": 10.9 } ]
}
```

`rows` are pre-sorted by `annual_vol` descending. Vols are decimals (0.19 =
19%); ranges are absolute dollar moves.

### moneyline (`POST /api/data/tools/moneyline`)

```json
{
  "chart_type": "moneyline", "ticker": "AAPL",
  "meta": { "ticker": "AAPL", "expiry": "2026-08-21", "current_price": 231.5 },
  "series": { "strikes": [ { "strike": 230, "call_open_interest": 1, "put_open_interest": 1,
                              "call_last": 1, "put_last": 1, "net_open_interest": 0,
                              "put_call_ratio": 1.0 } ] },
  "rows": []
}
```

Nine strikes centered on spot, sorted ascending. `rows` mirrors
`series.strikes`.

---

## Visual style guide (the terminal look)

The PNG charts are matplotlib, but the look is a design system you can
translate to any native charting stack. Don't replicate matplotlib mechanics —
match the palette, hierarchy, and semantics.

### Palette

| Token | Hex | Use |
| --- | --- | --- |
| `CHART_BG` | `#05070a` | Page/figure background |
| `AX_BG` | `#081016` | Plot area background |
| `PANEL` | `#0d171d` | Cards, tables, legend background |
| `GRID` | `#24444a` | Gridlines (thin, ~40–55% opacity) |
| `TEXT` | `#fff4c2` | Body text |
| `TEXT_STRONG` | `#fff9d9` | Price lines, emphasized text |
| `MUTED` | `#9fb0a8` | Axis labels, secondary text |
| `AMBER` | `#ffc94a` | Borders, accents, brand |
| `AMBER_HOT` | `#ffe66f` | Titles, primary overlay lines |
| `GREEN` | `#79ff9c` | Bullish / up / long |
| `CYAN` | `#57d9ff` | Secondary indicator lines |
| `RED` | `#ff695d` | Bearish / down / short |
| `VIOLET` | `#b28cff` | Tertiary lines |
| `ORANGE` | `#ffae57` | Quaternary lines |

Heatmap colormap (performance grid), diverging over monthly return with a
symmetric domain `±max(5, |max value|)`:
`#ff4d5a` (0.0) → `#172126` (0.42) → `#263237` (0.5) → `#79ff9c` (0.72) →
`#57d9ff` (1.0). Cell text is bold, ~8pt, dark-on-bright / light-on-dark.

### Typography and chrome

- Titles: uppercase, left-aligned, `AMBER_HOT`, bold; small muted bold subtitle
  under the title carrying period/provider/state context.
- Axis labels and ticks: `MUTED`, small (~9pt equivalents).
- Legends: `PANEL` background, `AMBER` border, `TEXT` labels.
- Every chart has a footer strip: left side green monospace uppercase stat line
  (e.g. `FAIR PRICE 321.66 | HIGH 344.57 | LOW 300.00`), right side amber
  monospace uppercase label (chart name or `UNDERLYING TERMINAL`).
- Primary price/portfolio lines get a subtle dark "glow" underlay in matplotlib;
  in native UIs a slight shadow or 2px darker stroke behind the line — or
  nothing — is fine.
- Last-value pills: rounded chips at the right edge labeling key levels
  (`VAH 344.57`), chip fill = level color, text = `CHART_BG`.

### Per-chart layout and encoding rules

**auction** — single price panel. Candles colored `GREEN`/`RED` by
close >= open; `TEXT_STRONG` close line over them. Horizontal levels: VAH
dashed `GREEN`, VAL dashed `RED`, POC dash-dot `AMBER_HOT` (thicker). Shade
VAL→VAH `AMBER` at ~5% opacity, POC→VAH `GREEN` ~11%, VAL→POC `RED` ~11%.
Right-edge pills for all three levels.

**performance** — heatmap grid (months x years) using the colormap above, plus
`Mean 5Y` / `Median 5Y` columns; print the % value in each non-null cell;
thin background-colored gaps between cells.

**regression** — two stacked panels ~3:1. Top: close (`TEXT_STRONG`), trend
line `AMBER_HOT`, ±1σ dashed `GREEN`/`RED`, σ-channel fill `CYAN` ~11%, EMA21
`CYAN`, EMA50 `VIOLET`, EMA200 `ORANGE`. Bottom: volume bars `GREEN`/`RED` by
up/down day, y-axis in millions (`42M`).

**ridge-growth** — three stacked panels ~3.4 : 1.0 : 1.15. Top: close +
fast EMA75 `CYAN`, base EMA150 `AMBER`, major SMA200 `MUTED`; shade the
background `GREEN` ~5% wherever `in_trade` is true; buy markers = green
up-triangles just below the bar's Low (Low x 0.985), sell = red down-triangles
just above High (High x 1.015); POC dash-dot line + faint VAL–VAH amber span.
Middle: equity curve, `GREEN` if total_return >= 0 else `RED`, dashed muted
line at the $10,000 starting capital. Bottom: a dashboard card/table (State,
Recommendation, Equity, Return, Drawdown, Flow, AMT location, caveat). Render
the `analysis_memo` markdown near the chart.

**flow-compass** — three stacked panels ~1.45 : 2.4 : 1.25. Top: close line
with fresh-long (green ^) / fresh-short (red v) markers. Middle: `flow_score`
histogram bars — `GREEN` above +25, `RED` below −25, `MUTED` between — with
`compass_signal` line `AMBER_HOT` on top; guide lines at 0 (solid), ±25
(dashed green/red), ±55 (dotted cyan/orange); y-range ±105. Bottom: horizontal
component bars (Volume, Trend, Momentum, Value, RVI), `GREEN` > +15 / `RED`
< −15 / `MUTED` between, value labels at bar ends, x-range ±105.

**torque** — four panels: price full-width on top (close + EMA75 `CYAN` +
SMA200 `AMBER`, "coiled-spring" band = SMA50 ±8% shaded `GREEN` if stage is
Coiled Spring else `AMBER`, ~7%); mid-left: 8-quarter revenue bars `CYAN` with
amber edges + gross-margin line `GREEN` on a secondary axis; mid-right:
operating-margin line `AMBER_HOT` with amber underfill; bottom full-width:
component score bars 0–100 colored by score (>=70 `GREEN`, >=50 `CYAN`, >=30
`AMBER`, else `RED`) with `score (weight%)` labels, plus a total-score gauge
line colored (>=75 `GREEN`, >=60 `CYAN`, >=45 `AMBER`, >=30 `AMBER_HOT`, else
`RED`) and a stage chip row (Coiled Spring green, Inflecting cyan, Proof Phase
amber, Renaming amber-hot, Extended red). When fundamentals are missing, show
an explicit "fundamental data unavailable — technicals only" state, not an
empty panel.

**portfolio** — single panel: each holding a thin line ~58% opacity cycling
[`CYAN`, `VIOLET`, `GREEN`, `RED`, `ORANGE`, `#8ef6d1`, `#d7a5ff`]; the
portfolio sum is the hero line `AMBER_HOT`, thick, with a faint amber underfill;
benchmark dashed `CYAN`. Currency y-axis (`$12,000`). Footer stats: return,
drawdown, vol.

**volatility** — horizontal bar list, already sorted desc; bar colors cycle
[`AMBER`, `GREEN`, `CYAN`, `RED`, `VIOLET`, `ORANGE`, `#8ef6d1`]; each bar
labeled `19.1%  |  1w ± 5.20  |  1m ± 10.90`; x-axis annualized vol %, headroom
~1.35x the max.

**moneyline** — mirrored open-interest bars per strike: calls `GREEN` up,
puts `RED` down (plot put OI negated), amber vertical line at spot, zero line
`TEXT_STRONG`; alongside, a strike-ladder table (Strike / Call OI / Put OI /
P/C) with amber header row and `PANEL` cells.

### Reference source (exact math and styling)

In the `underlying-analyzer-reboot` repo:

- `app/chart_data.py` — the data builders behind every payload above
- `app/charts.py` — matplotlib renders: palette constants, panel ratios, level styling
- `app/torque.py` — torque scoring + 4-panel dashboard render
- `app/tools.py` — moneyline data/render split (`build_moneyline_data`)

## Client tips

- OHLCV windows are full history for the period (1y daily ≈ 250 points, 2y ≈
  500, 5d ≈ 5 candles). Decimate client-side if your chart lib struggles.
- On short windows like `5d`, SMA-based series (`sma50`, `sma200`,
  ridge-growth `major_ma`) need a full lookback and come back short or empty —
  hide those overlays rather than erroring. EMA series (`ema21`/`ema50`/
  `ema200`, `fast_ma`, `base_ma`, `ema75`) always return values from the first
  bar but are weak estimates until the window exceeds their span; consider
  hiding long-span EMAs on very short windows. Auction levels compute over
  whatever sessions exist (nominally the trailing 21).
- Ridge-growth returns 3 datasets per ticker; on mobile fetch one ticker at a
  time and lazy-load windows.
- `provider` tells you where data came from (`yfinance`, `nasdaq`, mixed as
  `a+b`); surface `meta.errors` so partial watchlist failures are visible.
- All routes are public JSON POST, CORS open, no API key.
