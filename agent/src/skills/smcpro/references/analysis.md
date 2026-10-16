# Analysis - Steps 3 to 5

Extracted from `smcpro/SKILL.md` so the protocol always loads in full.

## Step 3 — Core SMC Definitions

### Displacement

Valid displacement requires ALL of the following:

- **Body size:** candle body ≥ 1.5× the **median** body of the prior
  20 candles on the same timeframe, AND in the top 20% of prior 20
  candle bodies (≥ 80th percentile).
- **Wick ratio:** combined upper + lower wick ≤ 40% of total candle range.
- **Decisive close:** close extends beyond the broken level by at least
  30% of that candle's total range (not just a wick through).
- **Volume confirmation (crypto):** displacement candle volume > 1.5×
  the 20-candle volume average.

No valid trade setup exists without displacement on 15M or 4H.

**Elite location exception:** If price is reacting from a fresh
unmitigated HTF POI with elite confluence context, a strong momentum
rejection / reclaim candle may substitute for the full displacement
template. Valid only when ALL are true:

- POI is Daily or 4H and clearly fresh
- Liquidity already swept or trapped at the location
- Kill zone / session supports the move
- Candle closes decisively back in the trade direction (≥ 30% of range)

If used, output `DISPLACEMENT EXCEPTION INVOKED` with evidence.

**Forex note:** Weak tick-volume confirmation reduces confidence but does
not auto-kill a structurally clean displacement.

### Mitigation Status

All OBs and FVGs must be classified:

| Status | OB Definition | FVG Definition | Trade Use |
|---|---|---|---|
| Unmitigated | Price has not returned to 50% of OB candle | Price has not filled any part of the gap | Valid |
| Partially mitigated | Price entered zone, no close beyond opposite extreme | Price entered but did not fully close through | Caution |
| Fully mitigated | Price closed beyond opposite extreme | Price closed through the whole gap | Dead — discard |

### Block Types & Priority

1. Fresh unmitigated OB
2. Rejection block (failed OB as rejection boundary; use for stop placement)
3. Breaker block (failed OB that flips polarity; valid only if the break
   happened with displacement per the definition above)
4. Partially mitigated OB
5. Fully mitigated OB/FVG → discard

### Internal Range Liquidity (IRL)

Identify stop clusters inside the range, not only at extremes.
Use IRL for trap recognition and SL refinement.

### Judas Swing

Counter-trend manipulation before the real move, usually around Asian
range or pre-kill-zone liquidity. If confirmed, the real trade is
opposite the Judas push.

### BTC Liquidity Grab Protocol

| Phase | What Happens | Required Response |
|---|---|---|
| Build-up | Price approaches clean high/low, round number, EQH/EQL | Flag high trap risk |
| Grab | Price wicks through the level | Do NOT treat the wick as entry |
| Confirmation | 15M closes back through the level in the real direction | This is the trigger |
| Re-entry | Thesis survives but you were stopped by the grab | Re-entry valid if close confirms back; SL beyond grab wick |

The wick alone is not confirmation. The close back through is.

### Second-Layer Confirmation Archetypes

Named refinements of LTF CONFIRMATION. Not extra mandatory gates.

**Hard cap:** all second-layer archetypes combined contribute at most
`+0.5` to confluence. They cannot create a valid setup, override missing
HTF bias, missing POI, or missing RR. Garnish, not structure.

**V-Shaped Recovery:** Valid only after price grabs liquidity from a major
key level or HTF POI. Requires: sweep → sharp close back through swept
level (≥ 30% of range) → displacement or strong rejection → entry still
at/inside a fresh OB/FVG.

**CISD (Change in State of Delivery):** Refined CHoCH. Log
`CISD CONFIRMED` when: 15M CHoCH/BOS closes through an internal delivery
level; confirmation candle creates/expands an FVG in the new direction;
delivery flips; price reacts from valid HTF POI or sweep context.

**Double Sweep / CTR:** Both sides of a local range swept, then price
reclaims in the intended direction with displacement, CISD, or decisive
close. May reduce trap risk one level (HIGH→MEDIUM, MEDIUM→LOW). Must not
auto-mark trap risk LOW without confirming reclaim.

---

## Step 4 — Top-Down Structure Analysis

Work strictly: **1D → 4H → 1H → 15M**.

### 4A. Daily — Macro Bias

Identify: market structure (HH/HL, LH/LL, ranging) · freshest unmitigated
HTF OB · open HTF FVGs · premium/discount location · current HTF delivery
· previous week high/low · clean unswept HTF liquidity levels (PWH/PWL,
monthly high/low, EQH/EQL, major swing) · any obvious daily Judas behavior.

Output:

- `DAILY BIAS: BULLISH / BEARISH / RANGING`
- `HTF DELIVERY: BULLISH / BEARISH / NEUTRAL`
- `HTF LIQUIDITY DRAW: [level] / NONE`

### 4B. 4H — Primary Bias

Identify: BOS or CHoCH · last confirmed structural shift (corrective vs.
possible HTF transition) · displacement quality (per Step 3) · ADX context
(>25 strong, 20–25 weak, <20 ranging) · active OBs/FVGs · rejection/breaker
blocks · liquidity above/below · clean unswept HTF liquidity draw · IRL ·
PDH/PDL · inducement · Judas behavior.

Output:

- `4H BIAS: BULLISH / BEARISH / RANGING`
- `4H STRUCTURAL SHIFT: NONE / CORRECTION / TRANSITION_EARLY / TRANSITION_CONFIRMED`
- `HTF DELIVERY: BULLISH / BEARISH / NEUTRAL`
- `HTF LIQUIDITY DRAW: [level] / NONE`

### 4C. 1H — Intraday Confirmation

Identify: whether 1H confirms 4H or conflicts · intraday CHoCH · whether
1H confirms or rejects any 4H transition attempt · active OBs/FVGs ·
rejection blocks · nearest liquidity targets · retracing vs. expanding ·
any 1H manipulation.

Output: `1H BIAS: BULLISH / BEARISH / RANGING`

### 4D. 15M — Entry Precision

Identify: micro BOS/CHoCH · entry-side displacement · whether 15M
supports HTF transition or is only a local bounce · entry OB · entry FVG ·
rejection block near entry · confirmation candle type · liquidity sweep
quality · IRL sweep · Judas / turtle-trap · V-shaped recovery · CISD ·
double sweep/CTR.

**Daily-trend resumption check:** If Daily is clear, 4H is in a
counter-trend correction, and 15M prints fresh CHoCH/BOS plus displacement
back in the Daily direction, this may qualify for the Daily Trend
Resumption exception (Appendix B (`references/appendices.md`)).

Mitigation rule: partially mitigated entry OB/FVG → downgrade confidence.
Fully mitigated → discard.

Output: `ENTRY READINESS: ACTIVE / WAITING / INVALIDATING FACTORS PRESENT`

15M readiness is diagnostic only. Final setup status is decided in Step 8.

### 4E. Reverse Read — Confirmation Bias Check

**Mandatory for full analyses.** After completing 4A–4D, answer three
questions before proceeding to Step 5.

**The problem:** The top-down sequence enforces order but not independence.
Once you form a Daily bias, you unconsciously read 4H, 1H, 15M to confirm
it. The hierarchy prevents cherry-picking timeframes but not confirmation
bias within the sequence.

**Question 1 — Blind read:** "If I had NOT seen the Daily chart first,
and I opened only 15M and 1H, what direction would I read?"

- Conflicts with Daily → `REVERSE READ CONFLICT: LTF reads [direction]
  without HTF anchor`
- Aligns → `REVERSE READ: ALIGNED`

**Question 2 — Counter-thesis:** "What is the strongest argument AGAINST
my current bias, using only the data I already fetched?"

- Write one sentence. If you cannot construct a counter-argument, flag
  `REVERSE READ: NO COUNTER-THESIS FOUND — possible anchoring`.

**Question 3 — Level swap:** "If my intended direction were reversed,
would I still consider the current price location a valid entry?"

- Yes → `REVERSE READ: LOCATION AMBIGUOUS — not a clean directional edge`
- No → `REVERSE READ: LOCATION DIRECTIONALLY SPECIFIC`

**Output (one line):**

- `REVERSE READ: ALIGNED — no conflict detected`
- `REVERSE READ: CONFLICT — [describe]`
- `REVERSE READ: ANCHORING RISK — [describe]`

**Rules:**

- `CONFLICT` does not auto-invalidate but reduces size to 50%. Must be
  stated in output.
- `ANCHORING RISK` requires re-examination of whether Daily bias was
  formed from structure or from a desired conclusion. If bias survives,
  proceed. If not, restart Step 4A.

**Crypto 5M note:** If 15M displacement closed too deep into the entry
zone and the setup remains valid, use 5M only for stop precision /
trigger refinement. Do not expand the framework beyond that.

---


## Step 5 — Volume Analysis

**Purpose:** Read current participation and urgency. Distinguish dormant
WAITING setups from active setups close to triggering.

**Core rule:** Price action is the gate. Volume is the
participation/urgency layer. Volume may upgrade monitoring priority,
support confidence, support passive-entry review, and warn that a WAITING
setup is waking up. Volume may NOT replace HTF bias, replace fresh POI
location, replace LTF confirmation, or make a full-size trade valid
without the required trigger.

### Volume Activity State

| State | Meaning | Trade Implication |
|---|---|---|
| `DORMANT` | Volume quiet or fading near POI | Lower urgency; do not anticipate |
| `ACTIVE` | Participation increasing, directionally agrees, trigger incomplete | `WAITING - ACTIVE`; monitor closely |
| `AGGRESSIVE` | Strong volume expansion + range/displacement pressure into/away from POI | `WAITING - AGGRESSIVE`; trigger may be close but do not chase |

Classification:

- `DORMANT`: no volume spike, weak range expansion, or volume contradicts.
- `ACTIVE`: volume > 1.3× 20-candle average, candle spread expanding,
  direction agrees.
- `AGGRESSIVE`: volume > 2× 20-candle average, range/displacement
  expanding, price at/near trigger or POI.

`ACTIVE` and `AGGRESSIVE` are monitoring states, not entry permission.

### Crypto Volume

Use real exchange volume from OKX/CCXT.

Check: displacement candle volume vs. 20-candle average · retracement
volume profile · OB creation volume · climax/exhaustion profile · volume
divergence at sweep or POI · volume delta between rejection and
continuation candles · open interest / liquidation context if available.

**VWAP (crypto only):**

| Check | Read |
|---|---|
| Price above VWAP | Bullish support for longs |
| Price below VWAP | Bearish support for shorts |
| Rejection from below | Bearish continuation |
| Rejection from above | Bullish continuation |
| Entry POI confluent with VWAP | Upgrade confidence |
| Entry POI on wrong side of VWAP | Caution |

Do not use VWAP on forex / indices / commodities.

Output:

- `CRYPTO VOLUME SOURCE: OKX / CCXT / NONE`
- `CRYPTO VOLUME RELIABILITY: HIGH / MEDIUM / NONE`
- `VOLUME: CONFIRMING / NEUTRAL / DIVERGING`
- `VOLUME ACTIVITY STATE: DORMANT / ACTIVE / AGGRESSIVE`
- Brief reason

### Forex Volume

**Structural limitation:** Spot FX has no centralized exchange volume.
Yahoo/yfinance spot volume is not reliable for forex confirmation.
This is a data limitation, not a framework flaw. The forex side of this
protocol is structurally thinner on volume than the crypto side.

**Do not fetch spot volume to "check" it.** The `get_market_data`
`_provenance` for a forex symbol carries `volume_note` stating the volume
column is not a participation signal, and Yahoo returns `volume = 0` on
every bar. Re-pulling the same FX series will never produce a real volume
number — treat volume as structurally unavailable and use the proxy below
(or drop it) instead of spending another call.

**Primary proxy:** CME FX futures volume. Fetch the relevant futures leg
with `search_symbol` to resolve the futures contract, then `get_market_data`.
Map the forex pair into base and quote currency legs.
Compare each leg against its recent baseline. Agreement between pair
direction and futures participation is stronger evidence than broker
tick volume.

Common CME proxy legs:

| Pair | Preferred Volume Proxy |
|---|---|
| GBPJPY | British Pound futures + Japanese Yen futures |
| EURGBP | Euro FX futures + British Pound futures |
| AUDJPY | Australian Dollar futures + Japanese Yen futures |
| USDJPY | Japanese Yen futures + Dollar Index / USD context |
| EURUSD | Euro FX futures + Dollar Index / USD context |

**Secondary proxy only:** Broker tick volume, session-open tick activity,
candle range/displacement expansion.

**Reliability labels:**

| Label | Requirement |
|---|---|
| `HIGH` | CME futures proxy confirms ≥ 1 liquid leg AND price action agrees |
| `MEDIUM` | CME unavailable but reputable tick volume and displacement agree |
| `LOW` | Only broker/yfinance/tick proxy exists |
| `NONE` | Volume is zero, missing, or contradictory |

**Volume divergence (both markets):** Bearish divergence = price makes
new high into liquidity/POI while volume is lower than prior comparable
swing high. Bullish divergence = new low with lower volume. Divergence
suggests possible exhaustion, not automatic reversal. For forex, if
reliability is LOW or NONE, mention divergence as observation but do not
award confluence.

If no CME proxy or reliable tick proxy is available:
`FOREX VOLUME SOURCE: NONE — spot FX volume is not centralized; volume
removed from confluence.`

Output:

- `FOREX VOLUME SOURCE: CME_PROXY / TICK_PROXY / NONE`
- `FOREX VOLUME RELIABILITY: HIGH / MEDIUM / LOW / NONE`
- `VOLUME: CONFIRMING / NEUTRAL / DIVERGING`
- `VOLUME ACTIVITY STATE: DORMANT / ACTIVE / AGGRESSIVE`
- Brief reason, naming the proxy used

---

