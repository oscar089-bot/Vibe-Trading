---
name: smc
description: Smart Money Concepts (ICT) signal engine. Uses the smartmoneyconcepts library to implement institutional-trading-school analysis of BOS, ChoCH, FVG, and order blocks (OB).
category: strategy
---
# Smart Money Concepts

## Purpose

The modern institutional trading school (ICT / Inner Circle Trader), built around the core idea of tracking the behavior of "smart money" (institutional capital):

| Concept | English | Meaning |
|------|------|------|
| Break of structure | BOS (Break of Structure) | Trend continuation signal |
| Change of character | ChoCH (Change of Character) | Trend reversal signal |
| Fair value gap | FVG (Fair Value Gap) | Price refill target |
| Order blocks | Order Blocks | Institutional order concentration zones |
| Liquidity | Liquidity | Stop-hunt target zones |

## Signal Logic

Direction from ChoCH + confirmation from BOS + filtering by FVG:
- **Long**: bullish ChoCH/BOS + bullish FVG exists
- **Short**: bearish ChoCH/BOS + bearish FVG exists
- **Stand aside**: no structural signal or conflicting directions

## Dependencies

```bash
pip install "vibe-trading-ai[smc]"
```

## Parameters

| Parameter | Default | Description |
|------|--------|------|
| swing_length | 10 | Window for swing high/low detection |
| close_break | True | Whether BOS/ChoCH requires a closing-price break |

## Signal Convention

- `1` = long, `-1` = short, `0` = stand aside

## Structural Engine Script

`scripts/smc.py` is a thin bridge over the same upstream
`smartmoneyconcepts` package, exposed as labelled JSON instead of a signal
Series. Use it for the mechanical structural read (FVG, BOS/CHoCH, order
blocks, liquidity, sessions) before drawing any conclusion.

It reads bars through Vibe-Trading's own loader layer
(`src.market_data.fetch_market_data` — the same registry and ordered
fallback chains the `get_market_data` tool uses), so a structural read and
the price bars share one source of truth. On first use it provisions its own
isolated venv (at `$VIBE_TRADING_HOME/tools/smc-venv`, created with `uv` when
available) holding `pandas` and `smartmoneyconcepts`, then re-runs itself
inside it. Progress goes to stderr; stdout stays pure JSON.

```bash
SMC=agent/src/skills/smc/scripts/smc.py

python3 "$SMC" analyze --bar-id 'AAPL.US' --interval 1D --count 250
python3 "$SMC" analyze --bar-id 'okx|BTC-USDT' --interval 4H --count 400 \
  --indicators fvg,swing_highs_lows,bos_choch --swing-length 20
python3 "$SMC" list
```

`--bar-id` accepts a Vibe-Trading symbol (`AAPL.US`, `BTC-USDT`, `700.HK`)
or the `source|symbol` form (`okx|BTC-USDT`, `yfinance|AAPL.US`) when an
explicit source is wanted. `--source` pins a loader; `--start`/`--end`
override the derived date window.

### Verbs

| Verb | Output |
|---|---|
| `analyze` | Requested indicators as labelled JSON records |
| `bars` | The prepared OHLCV frame — use when you want to compute something else in the same script |
| `list` | Indicator names, parameters and defaults |

### Indicators

| `--indicators` key | What it marks | Key output columns |
|---|---|---|
| `fvg` | Fair value gap — a 3-candle imbalance | `FVG` (1 bull / -1 bear), `Top`, `Bottom`, `MitigatedIndex` |
| `swing_highs_lows` | Fractal pivots over `swing_length` bars each side | `HighLow` (1/-1), `Level` |
| `bos_choch` | Break of structure vs change of character | `BOS`, `CHOCH`, `Level`, `BrokenIndex` |
| `ob` | Order block — the last opposing candle before an impulse | `OB` (1/-1), `Top`, `Bottom`, `OBVolume`, `Percentage` |
| `liquidity` | Clustered equal highs/lows within `range_percent` | `Liquidity` (1/-1), `Level`, `End`, `Swept` |
| `previous_high_low` | Prior period high/low and whether it broke | `PreviousHigh`, `PreviousLow`, `BrokenHigh`, `BrokenLow` |
| `sessions` | Which candles sit in a named session | `Active`, `High`, `Low` |
| `retracements` | Depth pulled back from the last swing | `Direction`, `CurrentRetracement%`, `DeepestRetracement%` |

Defaults: `swing_length=50`, `range_percent=0.01`, `close_break=true`,
`close_mitigation=false`, `join_consecutive=false`, `time_frame=1D`.
Override with `--swing-length`, `--range-percent`, `--close-break`,
`--close-mitigation`, `--join-consecutive`, `--time-frame`, and for sessions
`--session` plus `--start-time` / `--end-time` / `--time-zone`. Pass
`--full-series` to dump every bar for the state detectors, and
`--no-bootstrap` to forbid venv creation and fail instead.

### Reading the output

- **`MitigatedIndex` / `Swept` null means still open.** An unmitigated FVG or
  an unswept liquidity level is the live one; a filled one is history.
- **BOS continues, CHoCH reverses.** Bullish BOS = trend continuation through
  a prior swing high. Bullish CHoCH = the first break against the prior
  direction; it marks a character change, not a completed one.
- **`swing_length` is the single biggest lever.** 50 on daily data is
  structural/macro; 5-20 on intraday is tactical. State the value you used.
- **OB `Percentage` is a strength proxy, not a probability.**
- **Levels are geography, not signals.** Combine with the scalar side
  (`technical_indicators`) before drawing a conclusion.
- **Repainting caveat.** Swing detection looks `swing_length` bars forward,
  so the most recent `swing_length` bars cannot contain a confirmed pivot —
  the newest swing is provisional by construction. Say so.

Read `references/结构突破.md` for BOS/ChoCH semantics and
`references/订单块与缺口.md` for order blocks and FVG.

## Attribution

The `smartmoneyconcepts` package is MIT licensed, by Joshua Attridge and
contributors: https://github.com/joshyattridge/smart-money-concepts. It is
installed from PyPI at use time and is NOT vendored into this repository.
Only the bridge script is Vibe-Trading-authored.
