# Appendices A to C

Extracted from `smcpro/SKILL.md` so the protocol always loads in full.

## Appendix A — Watcher Phase Model

### Three Phases (no others valid)

| Phase | Meaning | Action |
|---|---|---|
| `THESIS_CONFIRMED_ENTRY_READY` | Fired trigger confirmed thesis; execution conditions exist now | Output full executable TRADE PLAN. Do NOT re-arm. Leave the fired watcher as triggered history. |
| `THESIS_ALIVE_WAITING` | Fired trigger did not produce entry, but thesis valid and next deterministic trigger can be named | Update the same watcher (`watcher_manager` `update`) to the next condition, setting `status` back to `active`. Confirm it is pollable via `watcher_manager` `get`. |
| `THESIS_DEAD` | Thesis invalid, missed, became a chase, no clean path, or fresh structure shifted with no new thesis | Cancel (`watcher_manager` `delete` or `cancel`) the fired watcher. Confirm it is gone/paused. |

Transition rules:

- `THESIS_CONFIRMED_ENTRY_READY` is terminal for the fired watcher.
- `THESIS_ALIVE_WAITING` must name the next deterministic trigger.
  If it cannot, it is `THESIS_DEAD`.
- `THESIS_DEAD` is terminal. A new setup requires fresh analysis.
- Banned phase labels: PULLBACK, CONFIRMATION, WEAKENED, SHIFTED,
  MISSED, INVALID_NO_TRADE. Distinctions live in `next_condition`
  prose, not phase labels.

### Supported Watcher Rule Types

- `price_enters_zone` with `low` / `high`
- `price_breaks_level` with `side` / `level`
- `price_crosses_level` / `level_reclaim` / `level_breakdown`
- `candle_close` with `side` / `level` / `timeframe`

Use `candle_close` (not `price_breaks_level`) when the trigger or
invalidation depends on a timeframe close. Use `price_breaks_level` only
for live intrabar alerts. Do not invent rule types.

Fields are authored through the **`watcher_manager` tool** (actions
`list` / `get` / `create` / `update` / `delete`). The stored model uses
snake_case, matching `src/watcher/models.py`:

- `pair` — a BARE symbol in the loader format (`BTC-USDT`, `EURUSD`,
  `AAPL.US`). Resolve a name with `search_symbol` first; never guess a suffix.
- `asset_class` — `crypto` / `forex` / `equity` / `commodity` / `index`
- `direction` — `LONG` / `SHORT`
- `timeframe` — e.g. `15M`, `1H`, `4H`
- `trigger_rules` — array of `{ type, ... }` (see rule types above)
- `invalidation_rules` — same rule shape; fires an `INVALIDATED` event
- `invalidation_conditions` / `waiting_trigger` / `analysis_context` — carried
  alongside the rules for the re-analysis handoff; `analysis_context` is where
  `watcher_phase`, `next_condition` and `thesis_continuity` live
- `session_id` / `thread_id` — the conversation the fired watcher wakes

There is no separate Issues file or authoring skill here: a watcher is a
record in `watcher_manager`, and its health is rendered on the **Market
Watchers panel on the Scheduled page** (or via `watcher_manager` `list`, which
reports `is_pollable`). Create once per pair/setup; update the existing
watcher in place rather than creating a second one for the same setup.

### Multi-Step Sequences

Arm only the next unmet step, not the final condition in the sentence.

Example: `Wait for SOL to retrace into 66.06–66.93, reject, then close a
15M candle below 66.06.`

- First watcher: `price_enters_zone` at 66.06–66.93.
- The watcher's `analysis_context` records the later requirement:
  `next_condition: check rejection/CISD; update same watcher to a 15M
  candle_close below 66.06`.

### Watcher-Trigger Re-Analysis

When an existing watcher fires:

**Choose response mode:**

- `FULL STRUCTURAL RE-ANALYSIS`: for first thesis-confirmation triggers,
  invalidations, weakened/shifted structure, unclear phase, or unreliable
  prior thesis.
- `FAST EXECUTION CHECK`: only when stored `watcher_phase` is
  `THESIS_ALIVE_WAITING` and prior analysis already confirmed thesis.
  Do not restart full top-down unless fresh data shows invalidation or
  material shift.

**FAST EXECUTION CHECK is not fast entry approval.** Fast means focused,
not shallow. Check: fresh data · exact fired condition · thesis survival ·
invalidation · location/chase risk · LTF rejection/CISD/displacement ·
volume/participation · RR from current entry to targets · management ·
invalidation. Must be willing to output WAITING, THESIS_DEAD, or
THESIS_ALIVE_WAITING.

**Outcomes:**

- Execution conditions present → `THESIS_CONFIRMED_ENTRY_READY` +
  full TRADE PLAN (direction, execution mode, entry, stop, TP1–TP3,
  RR, size, invalidation, management, reason). Do not re-arm.
- Thesis alive, one condition missing → rewrite same watcher,
  `THESIS_ALIVE_WAITING`, name `next_condition`.
- Missed, chase, no clean next condition → `THESIS_DEAD`, close watcher.
- Fresh structure shift → `FAST CHECK ESCALATED TO STRUCTURAL
  RE-ANALYSIS`, then classify based on new evidence.

**Continuity rules:**

- A fired watcher is an alarm bell, not a cage. Fresh evidence overrides
  stored thesis when structure shifted.
- If fresh structure reveals a better POI, FVG, liquidity draw,
  invalidation, or confirmation path, adapt and update the same watcher.
- Do not chain endless WAITING updates. Re-arm only for the next concrete
  execution phase unless structure genuinely shifted.
- If previous watcher was thesis-confirmation, the pullback/retest is
  `same thesis, next execution phase`, not a new thesis. Set
  `thesis_continuity: same_thesis_next_execution_phase`.
- If no concrete next phase after confirmation, classify `THESIS_DEAD`.

**Required `analysis_context` fields on re-arm:** `watcher_phase` ·
`previous_watcher_phase` · `next_condition` · `thesis_continuity`
(`same_thesis_next_execution_phase` or `new_thesis_due_to_structure_shift`).

**Final answer must include:** `WATCHER PHASE: [phase]` and whether this
is same thesis / next phase or new thesis.

**Watcher line format:**

- Waiting: `re-armed fired watcher: [watcher_id]`
- Ready: `triggered history kept: [watcher_id]`
- Dead: `closed fired watcher: [watcher_id]`

Arm, update and cancel the watcher through the `watcher_manager` tool. Do not
print a fenced JSON block as the primary watcher action — the tool call is the
action; prose only reports what it did.

---


## Appendix B — Exception Registry

Maximum **2 exceptions per trade.** Declared **before** confluence scoring.
If a third would be needed → `WAITING` or `INVALID`.
Output must list every invoked exception. If cap hit:
`EXCEPTION CAP REACHED — no further exceptions available`.

### 1. Displacement Exception

Elite location substitute for full displacement template. See Step 3.

### 2. Passive Entry Exception

Reduced starter at elite HTF location before full 15M confirmation.
See Step 8. Max 1 per session. Cooldown after failure.

### 3. TP2 RR Validation Exception

TP1 becomes management target; TP2 becomes formal RR validator.
Requires: structural SL methodology satisfied · High Probability or Elite
rating or confluence ≥ 6.0 · TP1 nearby because it is first liquidity
pocket, not poor entry · TP2 clean, realistic, unmitigated, aligned ·
path to TP2 does not force through dense opposing liquidity, major HTF
POI, or Tier 1 risk · TP2 gives ≥ 1:2 RR (or 1:1.5 with another valid
RR exception) · clear management plan at TP1.

Output: `TP2 RR VALIDATION INVOKED` · TP1 labelled `management target /
partial` · TP2 labelled `formal RR validation target` · RR to both shown.

### 4. Ranging Market RR Exception

Relax TP1 from 1:2 to 1:1.5. Requires: Daily explicitly ranging ·
4H explicitly ranging · confluence ≥ 6.0 · price at true range boundary ·
liquidity sweep at boundary confirmed.

### 5. HTF Location RR Exception

Relax TP1 from 1:2 to 1:1.5 outside pure ranges. Requires: High
Probability or Elite · fresh unmitigated HTF POI · location doing the
real work · path to target clean.

### 6. Daily Trend Resumption Exception

Passes HTF BIAS gate when Daily is directional, 4H in counter-trend
correction, 15M prints fresh CHoCH/BOS + displacement back in Daily
direction, 15M POI fresh, 4H has not reversed opposite. See Step 6.

---


## Appendix C — Confidence System

Two separate layers. Never mixed into one number.

### Layer 1 — CONFIDENCE BAND

Ordinal quality label from confluence score only.

| Band | Confluence | Meaning |
|---|---|---|
| `LOW` | 4.0–5.0 | Minimum passing; some gaps or conflicts |
| `MEDIUM` | 5.5–7.0 | Clean structure, no gaps |
| `HIGH` | 7.5–10.0 | Strong multi-TF alignment |
| `ELITE` | 10.5+ | Exceptional, no gaps |

### Layer 2 — CONFIDENCE CAVEATS

Separate risk layer. Does NOT change the band. Adjusts execution and size.

Caveats reported: TRAP RISK · REVERSE READ · NEWS STATUS · EXCEPTIONS USED.

| Caveat Combination | Effect |
|---|---|
| Any HIGH caveat (trap HIGH or news HIGH RISK) | Cannot `PLACE LIMIT NOW`; size 50% |
| REVERSE READ: CONFLICT | Size 50%; mandatory counter-thesis in output |
| REVERSE READ: ANCHORING RISK | Restart Step 4A |
| 2 exceptions used | Size 50% |
| LOW band + any HIGH caveat | WAITING only; cannot be VALID |

Output format:

- `CONFIDENCE BAND: [band] — ordinal quality label, not a probability estimate`
- `CONFIDENCE BASIS: [one-line reason]`
- `CONFIDENCE CAVEATS: [list]` — **only when at least one caveat is active**

Do NOT output a numeric probability percentage.

If asked "what's the probability": state the band · state sample size in
that band from recorded trades (or "no sample yet") · state explicitly:
"This framework does not produce calibrated probabilities. The band is an
ordinal quality label derived from structural confluence."
