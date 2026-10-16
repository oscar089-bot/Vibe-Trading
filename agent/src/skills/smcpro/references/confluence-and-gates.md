# Weighted Confluence and Trade Gate - Step 6

Extracted from `smcpro/SKILL.md` so the protocol always loads in full.

## Step 6 — Weighted Confluence & Trade Gate

### Structural Gate

Three mandatory pillars. All three required before `VALID`.

| Pillar | Definition |
|---|---|
| `HTF BIAS` | 4H aligns with the dominant tradable narrative, Daily Trend Resumption exception applies, or `HTF TRANSITION: CONFIRMED` validates a reversal narrative |
| `POI LOCATION` | Fresh unmitigated OB or FVG in the trade direction |
| `LTF CONFIRMATION` | 15M BOS/CHoCH plus displacement per Step 3 |

**Note on pillar independence:** the three pillars are correlated reads
of the same price series (see Design Intent). The gate enforces different
types of evidence — bias, location, trigger — but not independent
verification. See Design Intent for the full framing.

Gate logic:

- Missing `HTF BIAS` → `INVALID`
- Missing `POI LOCATION` → `INVALID`
- Missing only `LTF CONFIRMATION` → may be `WAITING`, but only if the
  exact trigger can be named
- Cannot identify 3 distinct contributing factors → `WAITING`, not `VALID`

### HTF Narrative Rule

The tradable narrative may be: continuation with the Daily trend, or a
true reversal narrative after sweep + CHoCH + displacement on 4H.
Daily remains the macro anchor; 4H defines the actual tradable leg.

### HTF Transition / Reversal Watch Mode

Use when the proposed trade direction conflicts with the current HTF
label but fresh evidence suggests transition.

Mandatory output fields when transition evidence exists:

- `HTF TRANSITION: NONE / EARLY / WATCH / CONFIRMED`
- `TRANSITION DIRECTION: BEARISH_TO_BULLISH / BULLISH_TO_BEARISH / NONE`
- `TRANSITION FACTORS: [list]`
- `TRANSITION VERDICT: BOUNCE_ONLY / WATCH_FOR_REVERSAL / REVERSAL_CONFIRMED`

**Transition evidence factors** (score only objective evidence):

1. `HTF STRUCTURE BREAK` — close beyond last meaningful swing with
   displacement. Wick through is not enough.
2. `MAJOR HTF LIQUIDITY SWEEP + RECLAIM` — sweep then immediate decisive
   close back through with displacement.
3. `INSTITUTIONAL HTF LOCATION` — reacting from monthly/weekly/daily
   demand/supply, yearly/quarterly open, quarterly FVG, major OB, deep
   discount/premium, or major historical shelf.
4. `ACCUMULATION / DISTRIBUTION EVIDENCE` — repeated failed
   breakdowns/breakouts, multiple stop hunts, range formation after
   extended trend, breakout with participation.
5. `IMPULSE QUALITY SHIFT` — new-direction moves show displacement;
   old-direction pullbacks become shallow/overlapping.
6. `MTF CONFIRMATION STACK` — 4H/Daily starts transition, 1H confirms,
   15M confirms entry timing.
7. `NARRATIVE / CATALYST SHIFT` — optional supporting factor only.
   Must not replace price structure.

**Classification:**

- `EARLY`: 1–2 factors, no 4H/Daily structure break confirmed. Output
  usually `WAITING` or `NO TRADE`.
- `WATCH`: ≥ 3 factors including structure break or major sweep+reclaim,
  plus institutional location. LTF confirmation incomplete. Create/update
  watcher for next deterministic confirmation.
- `CONFIRMED`: 4H/Daily confirms with close + displacement · price
  reclaims/holds broken level · transition starts from valid HTF location ·
  1H/15M confirms continuation · RR clean.

Only `CONFIRMED` may satisfy `HTF BIAS` as a reversal narrative.

**Transition judgment explanation** (only when EARLY, WATCH, or CONFIRMED):

- `WHY NOT JUST A BOUNCE`: one sentence.
- `HTF TRANSITION EVIDENCE`: exact HTF facts.
- `MISSING CONFIRMATION`: what is still required.
- `TRANSITION INVALIDATION`: level/condition that proves it wrong.

If the explanation cannot defend the label, downgrade it.

**Hard reversal safety rules:**

- 15M CHoCH/BOS alone never confirms an HTF reversal.
- POI tap, support bounce, oversold, divergence, or wick sweep alone
  never confirms an HTF reversal.
- If Daily/4H remain bearish and only LTFs turn bullish, label
  `BOUNCE_ONLY` or `EARLY`.
- In counter-bias conditions, passive entries are prohibited unless the
  passive-entry exception is fully satisfied and logged.
- If transition evidence is promising but incomplete, output `WAITING`
  with the exact next confirmation needed.
- Do not upgrade to `VALID` unless the full execution chain passes.

### Daily Trend Resumption Exception

May pass the `HTF BIAS` gate when: Daily bias is clearly directional ·
4H is in counter-trend correction · 15M prints fresh CHoCH/BOS +
displacement back in Daily direction · 15M entry POI is fresh and
unmitigated · 4H has not already reversed structure in the opposite
direction. See Appendix B (`references/appendices.md`) for invocation rules.

### Weighted Confluence Table

Minimum tradable score: **4.0**
Minimum distinct contributing factors: **3**

> Weights are structured heuristics reflecting relative importance.
> They are not statistically calibrated. A score of 9/15 is precise
> but not "accurate" in a statistical sense. Treat scores as structured
> reasoning aids, not measured probabilities.

| Factor | Weight |
|---|---:|
| Daily bias aligns with trade direction | 1.5 |
| Daily trend resumption exception (bonus) | +1.0 |
| HTF transition confirmed as reversal narrative | 1.5 |
| HTF transition watch confluence (not yet confirmed) | 0.5 max |
| 4H structure confirms with displacement | 1.5 |
| 1H confirms / does not conflict | 1.0 |
| Price in correct premium/discount zone | 1.0 |
| Fresh unmitigated OB at entry zone | 1.0 |
| Unmitigated FVG at/near entry zone | 0.5 |
| Liquidity sweep before entry signal | 1.0 |
| Clean unswept HTF liquidity draw aligns with trade path | 0.5 |
| Second-layer confirmation (CISD / V-recovery / CTR, capped) | 0.5 max |
| Inside active kill zone | 1.0 |
| Volume confirms | 0.5 |
| 4H trend strength confirmed by ADX | 0.5 |
| Crypto VWAP alignment at POI | 0.5 |
| News environment clear | 0.5 |

**Maximum score: 15.0**

Scoring rules:

- HTF liquidity draw: award only for clean, unswept, relevant levels
  (PWH/PWL, monthly, major swing, EQH/EQL). Do not double-count with
  liquidity sweep.
- HTF transition: award confirmed OR watch, never both. Do not
  double-count the same 4H displacement as both transition confirmation
  and 4H structure confirmation.
- Second-layer: CISD, V-recovery, CTR share one capped 0.5 bonus.
- Volume: crypto from real exchange volume. Forex only when reliability
  is HIGH or MEDIUM. If LOW or NONE, do not award.
- Volume activity state changes monitoring urgency, not confluence points.

### Rating & Size

| Score | Rating | Base Risk |
|---|---|---|
| 4.0–5.5 | Standard | 1.0% |
| 6.0–8.0 | High Probability | 1.5% |
| 8.5+ | Elite | 2.0% |

Consistency: `RATING` must align with `CONFLUENCE` and `CONFIDENCE BAND`.
Do not label "Elite" if confirmation is pending or any exception was invoked.

See Appendix C (`references/appendices.md`) for the full confidence system.

---

