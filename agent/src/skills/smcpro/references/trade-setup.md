# Trade Setup - Step 8

Extracted from `smcpro/SKILL.md` so the protocol always loads in full.

## Step 8 — Trade Setup

### Setup Status Definitions

| Status | Definition |
|---|---|
| `VALID` | All gates met, confluence ≥ 4.0, ≥ 3 distinct factors, displacement confirmed, POI alive, acceptable RR, ≤ 2 exceptions |
| `WAITING` | HTF bias and POI valid, but exact LTF trigger not printed |
| `WAITING - ACTIVE` | Still WAITING; volume ACTIVE; market waking up near trigger/POI |
| `WAITING - AGGRESSIVE` | Still WAITING; volume AGGRESSIVE; trigger may be close but confirmation still required |
| `INVALID` | Cannot become valid under current conditions |

### WAITING Trigger Rule

Allowed: `Price must reclaim 94,200 with a 15M displacement close above
the OB → VALID`

Not allowed: `Price needs to pull back` · `Waiting for confirmation`

If you cannot write a trigger that specific, the setup is `INVALID`,
not `WAITING`.

### WAITING Activity Rule

| Volume Activity | Label | Message |
|---|---|---|
| DORMANT | `WAITING` | Planned but not urgent |
| ACTIVE | `WAITING - ACTIVE` | Participation building; monitor trigger |
| AGGRESSIVE | `WAITING - AGGRESSIVE` | Trigger may be close; no chase |

Rules: use ACTIVE/AGGRESSIVE only if HTF bias and POI are valid. Never
bypass invalidation. Setup remains inactive until activation criteria print.

### WAITING Blueprint Rule

If WAITING, still provide the full planned setup: ENTRY ZONE · STOP LOSS ·
TP1 · TP2 · TP3 · MOVE BE AT · POSITION SIZE · WAITING TRIGGER.
The missing piece is activation, not planning.

### Execution Mode

Every VALID or WAITING setup must state one of:

- `PLACE LIMIT NOW`
- `PLACE LIMIT NOW (REDUCED)`
- `WAIT FOR CONFIRMATION`
- `NO TRADE`

If a sweep, reclaim, displacement close, or CHoCH is still required,
execution mode must be `WAIT FOR CONFIRMATION`.

If any HIGH caveat is active (trap HIGH or news HIGH RISK), execution
mode cannot be `PLACE LIMIT NOW`.

### Universal SMC Execution Chain

```
HTF thesis
→ price enters POI / retest zone
→ wait for LTF rejection or CISD
→ enter only if R:R is still clean
→ manage around liquidity targets
```

- Any step missing → not VALID yet.
- HTF thesis exists but price not at POI → WAITING.
- Price at POI but no LTF rejection/CISD/displacement → WAITING - ACTIVE.
- Rejection confirms but RR no longer clean → INVALID.
- Rejection confirms and RR clean → VALID.
- Watcher alert, level touch, POI tap = alert only, not the trade.

### Counter-Bias Forex POI Rule

When Daily and 4H are mixed, counter-trend, or unclear:

- No passive FVG, OB, or POI entries.
- POI touch gives location only, not trade permission.
- Status stays WAITING until LTF confirmation prints.
- Do not classify VALID only because price tapped a valid POI.

For a long in mixed/counter-bias conditions, require:
sweep/tap into POI → bullish rejection → 15M close/reclaim above
displacement midpoint/FVG top/OB boundary → retest hold → then entry
permission may be considered.

For a short: mirror the above.

If only the POI touch happened:

- `SETUP: WAITING`
- `EXECUTION MODE: WAIT FOR CONFIRMATION`
- `PASSIVE ENTRY STATUS: NOT ELIGIBLE — HTF bias mixed/counter-bias`
- Deterministic WAITING TRIGGER

### Passive Entry Exception

Narrow exception for elite HTF-location trades likely to react before
textbook 15M confirmation prints.

All conditions must be true:

- Setup rated High Probability or Elite
- Daily and 4H aligned, or Daily Trend Resumption clearly applies
- Entry at fresh unmitigated Daily or 4H POI
- True first touch since creation
- Not mid-range; edge from HTF location itself
- Meaningful liquidity sweep or trap already occurred into the POI
- Kill zone / session supports immediate response
- Stop placement structurally clean
- TRAP RISK not HIGH
- NEWS STATUS not HIGH RISK
- REVERSE READ must be ALIGNED

**Session cooldown:** Maximum **1 passive entry per session**. If the
previous passive entry in this session failed (time-failed or stopped),
the next passive entry requires explicit user override.

If all conditions hold:

- Execution mode: `PLACE LIMIT NOW (REDUCED)`
- Initial passive size: 25%–50% of normal planned size
- Remaining size added only after exact 15M confirmation trigger prints
- If reaction is weak/sluggish/fails to confirm within 2–4 completed
  15M candles after fill (2 for sharp session setups, 4 for exceptional
  HTF locations with slower expansion), treat as time-failed and stand down

If used, output: `PASSIVE ENTRY EXCEPTION INVOKED` · status · why location
qualifies · why reduced staging allowed · exact trigger for adding size ·
exact reduced fraction.

Even when not used, output: `PASSIVE ENTRY STATUS: NOT ELIGIBLE — [reason]`

### Entry

- Entry at or inside the identified unmitigated 15M OB/FVG.
- Default: **proximal edge** (closest to current price).
- Midpoint: only with passive-entry exception.
- Distal: only if retest of distal edge is explicitly part of WAITING
  TRIGGER.
- If the trade plan does not state which edge, it is incomplete.

### Stop Loss — Structural Invalidation

**Step 1 — Identify invalidation timeframe:**

| Trade Driver | Invalidation TF |
|---|---|
| 4H structure + 15M entry | 4H swing |
| 1H structure + 15M entry | 1H swing |
| 15M scalp only | 1H nearest structural level |
| Daily trend resumption | 4H counter-trend extreme |

**Step 2 — Distinguish micro vs. structural levels:**

A level is **micro** (unsafe for SL) if ANY: single-candle swing with no
HTF significance · wicked through in last 10–20 candles · within
0.15%–0.25% of entry (crypto) or 5–8 pips (forex majors) · OB midpoint
rather than extreme · formed in last 4–8 candles with no HTF context ·
inside active FVG/OB/IRL zone.

A level is **structural** (valid SL candidate) only when ALL: clear swing
on invalidation TF · beyond visible stop-side liquidity · distance ≥ 0.20%
(crypto) or 10+ pips (forex majors) · breaking it flips the bias · not
the obvious retail level (EQH/EQL, round number, recent unswept swing).

**Step 3 — Stop-side liquidity and sweep buffer:**

Include unswept stop-side liquidity. If rejection block boundary is
fresher than broad OB, use it. If liquidity grab occurred on stop side,
SL beyond grab wick.

Primary buffer: `0.25 × ATR(20)` of the invalidation timeframe beyond
the structural level.

Fallback when ATR unavailable:

| Market | Buffer |
|---|---|
| Crypto alts (SOL, AVAX) | 0.15%–0.25% |
| BTC / ETH | 0.10%–0.15% |
| Forex majors | 3–5 pips |
| Forex crosses (GBPJPY) | 5–8 pips |
| Gold / XAUUSD | $2–$3 |

If ATR buffer would exceed 2× fallback, reconsider the invalidation TF.
State when fallback was used.

**Step 4 — RR integrity check:**

After placing SL, recalculate RR. TP1 must meet minimum (1:2 standard,
1:1.5 only with valid exception). If TP1 fails and no exception applies:

1. Wait for better entry (price closer to invalidation zone)
2. Reduce size (keep wider stop, same dollar risk)
3. Skip

Never tighten SL into micro territory to salvage RR.

**Step 5 — Anti-anchoring check:**

"If I had no prior levels marked, where would I say the thesis is
wrong?" · "Is this level likely to be swept as normal behavior?" ·
"Am I anchoring to a previous read?"

If defending a level from before the current session's price action,
assume anchoring risk. Reset.

**SL output requirement:** `SL RATIONALE` must state invalidation TF ·
structural level · why structural not micro · stop-side liquidity ·
buffer used · post-SL RR.

**BTC rule:** Never place stops at obvious round numbers or clean swing
levels. After a grab, SL beyond the grab wick. Widen and reduce size
proportionally.

### Take Profit Levels

| Target | Description | RR |
|---|---|---|
| TP1 | Nearest clean liquidity / management target | Default formal validator |
| TP2 | 4H swing, major HTF level, clean external liquidity | May validate RR only under TP2 exception |
| TP3 | Major external liquidity sweep | 1:5+ |

Verify target is still unmitigated.

### RR Exceptions

See Appendix B (`references/appendices.md`) for: TP2 RR Validation · Ranging Market RR · HTF Location RR.

### Trade Management

- At 1:1 → move SL to breakeven
- At TP1 → close 50%
- Trail rest toward TP2/TP3
- 15M CHoCH against position → consider full exit
- Target becomes mitigated before hit → reassess

### Drawdown-Based Position Scaling

| Condition | Override |
|---|---|
| 0–1 consecutive losses | Normal size |
| 2 consecutive losses | 50% size |
| 3 consecutive losses | Session stop / observation only |
| First trade back after stop | 50% size |
| Back to full size | Only after 2 consecutive wins |

This override takes priority over the base confluence size table.

### WAITING Watcher Rule

For every WAITING setup, arm a watcher with the `watcher_manager` tool
(`create`) before the final answer.

Key rules:

- Complete analysis and commit to setup status FIRST. Do not write a
  watcher before the final decision.
- If no active watcher exists for this pair/setup, create one. If one
  exists, update it in place. Do not create duplicates.
- For multi-step sequences, arm only the next unmet step.
- Confirm via `watcher_manager` `get` that the watcher is pollable
  (`is_pollable: true`), then report the watcher id and what it watches.

**A watcher that omits a critical detail is rejected at creation.** Supply
all of: a bare `pair` (never source-prefixed — `BTCUSDT`, not
`okx|BTCUSDT`); `asset_class`; `direction`; a servable `timeframe` (`1m`, `5m`,
`15m`, `30m`, `1h`, `4h`, `1d`, `1w` — omit to default to `1d`); a plain-English
`waiting_trigger`; and at least one fully-specified deterministic rule (numeric
`level` + `side`, or numeric `low`/`high`). An `entry_zone` alone derives a
`price_enters_zone` rule. The rejection names the field and the fix, so correct
it and retry rather than guessing.

**Choose the data source deliberately.** `source` names the venue the watcher
polls through and defaults to `yfinance`. Set it to the venue the analysis used
so the watcher prices the same instrument you read: `okx`/`binance`/`ccxt` for
exchange-native crypto pairs, `yfinance`/`yahoo` for equities, forex and
indices, `tushare`/`akshare` for A-shares, `mt5` for a local forex terminal.
Use `auto` to let the loader registry route by market. An unknown source is
rejected at creation. Keep `pair` bare — the source is a separate field, never a
prefix.

See **Appendix A (`references/appendices.md`)** for the full watcher model, rule types, and
watcher-trigger re-analysis protocol.

---

