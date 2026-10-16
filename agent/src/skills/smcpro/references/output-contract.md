# Compact Output Contract - Step 10

Extracted from `smcpro/SKILL.md` so the protocol always loads in full.

## Step 10 — Compact Output Contract

Do the full analysis internally. The visible answer must be compact,
plain-English, and decision-first.

**Length targets:**

- Normal analysis: 12–22 short lines
- Complex transition/reversal: 22–30 lines max
- Full audit/reasoning dump: only when user explicitly asks

### Required Header (every output)

Combine into 4–5 lines:

```
PAIR: [pair] | MODE: [FULL PROTOCOL / FAST GATE / QUICK READ]
SETUP: [VALID / WAITING / INVALID / NO TRADE] | DIRECTION: [LONG / SHORT / NONE] | NARRATIVE: [TREND_CONTINUATION / DAILY_RESUMPTION / HTF_REVERSAL_CONFIRMED / BOUNCE_ONLY / NO_TRADE]
CONFLUENCE: [score]/15 — [strong / moderate / weak / n/a] | BAND: [LOW / MEDIUM / HIGH / ELITE] | RATING: [Standard / High Probability / Elite / No Trade]
REVERSE READ: [ALIGNED / CONFLICT / ANCHORING RISK]
```

`CONFIDENCE CAVEATS` line is printed **only when at least one caveat is
active.** If no caveats, omit the line entirely. Do not print
`CAVEATS: none`.

`CONFIDENCE BAND` is an **ordinal quality label, not a probability
estimate.** If the user asks for a probability, state the band, the
sample size in that band (or "none yet"), and explicitly state the
framework does not produce calibrated probabilities.

### Compact Bias Snapshot

Print only bias details that affect the decision:

- `DAILY`: bias + simple meaning
- `4H`: bias + simple meaning
- `1H`: bias + simple meaning
- `HTF TRANSITION`: only if not NONE, with the important reason

Example: `4H: still bearish — sellers control the bigger move`

### Core Reasons

3–5 bullets max. Each combines evidence + simple meaning. Include
confluence score and strongest factors. Include trap/news/volume/funding
only if materially decision-relevant.

For HTF transition outputs, include: WHY NOT JUST A BOUNCE (one sentence)
· CONFIRMING EVIDENCE (2–3 bullets) · MISSING CONFIRMATION (one sentence)
· INVALIDATION (level/condition).

### If SETUP = VALID → TRADE PLAN

- `ENTRY`: exact price/range, entry edge, execution mode
- `STOP`: exact level + simple reason
- `TP1 / TP2`: exact levels; TP3 only if useful
- `R:R`: to TP1 and TP2
- `POSITION SIZE`: normal/reduced + why
- `INVALIDATION`: what proves the trade wrong
- `DO NOT CHASE`: only if price already away from entry

### If SETUP = WAITING → WAITING PLAN

- `WAITING STATE`: DORMANT / ACTIVE / AGGRESSIVE + simple meaning
- `TRIGGER`: exact condition
- `ENTRY AREA`: level/range + entry edge
- `STOP / TARGETS`: planned levels only
- `INVALIDATION`
- `NO ACTIVE TRADE YET`: explicit
- `WATCHER`: watcher id, status, trigger monitored

If ACTIVE or AGGRESSIVE, add: WHY ACTIVE (one sentence) · WHAT MUST PRINT
NEXT (one sentence) · DO NOT CHASE NOTE (if relevant).

### If SETUP = INVALID → WHY NO TRADE

- `WHY INVALID`: one clear reason
- `MISSING REQUIREMENT`
- `WHAT MUST CHANGE`
- `KEY RISK`: only if relevant

Do not fabricate a trade blueprint for an invalid setup.

### Exception Logging

If any exception is used, include `EXCEPTIONS INVOKED` section:

- `EXCEPTIONS USED: [N] of 2 maximum`
- If N = 2: `EXCEPTION CAP REACHED`
- List each: e.g. `DISPLACEMENT EXCEPTION INVOKED — [reason]`

If no exceptions, do not print the section.

### Plain-English Rule

Explain trading terms simply the first time they appear. After each core
reason, say what it means for price or market control. Avoid long theory.
Write like the user needs the useful result quickly. Keep numbers precise,
explanation human.

### Style Rules

Clean section headers · short labeled bullets · precise numbers · concise
reasoning · simple terms · useful for a trader not deep in SMC language ·
no hedging between WAITING and INVALID.

### Do Not Print By Default

Full STEP GATES · full PRE-OUTPUT AUDIT · every POI/OB/FVG found · long
confluence tables · full environment sections · repeated labels · raw
watcher JSON. Include HEAT STATUS only when account context was supplied
or execution requested.

---

