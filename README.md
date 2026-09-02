# Coastal American Airways — Gate Utilization (gate-ops)

Client-side gate/stand utilization viewer. Renders directly from JSON in the
browser (SVG, zoomable/pannable) — no server-side chart rendering, and no
LLM context spent generating or reviewing chart images. Companion site to
the timetable app, kept in a separate repo since this one iterates
continuously during schedule building while the timetable is a stable
per-Finalize deliverable.

**Live once deployed:** `https://<your-github-username>.github.io/caa-gate-ops/`

## What's here

```
index.html          the whole app (single file, no build step)
coastal_logo.png     header logo
data/
  gate_manifest.json   list of available schedules, newest first
  gate_<id>.json       one file per schedule (claims + gate assignment)
tools/
  generate_gate_json.py   the generator — reuses build_claims()/assign_gates()
                          from Gate_Utilization_Process.md verbatim; swaps the
                          matplotlib render for a JSON export. Kept in this
                          repo for reference/history; the copy of record for
                          building against a live schedule lives in that
                          schedule's own chat + PK.
```

## Updating data for a new schedule

1. In the schedule's build chat, run `tools/generate_gate_json.py`'s
   `export_schedule()` against that schedule's real data (see the
   `load_real_schedule()` docstring in the script for the expected input
   shape).
2. Drop the resulting `gate_<id>.json` into `data/`.
3. Prepend an entry to `data/gate_manifest.json` (`write_manifest()` does
   this, matching the same newest-first pattern the timetable app's
   `schedules.json` uses).
4. Commit + push. No build step, no server — Pages serves the static files
   directly and the browser does the rest.

## A bug fixed here, worth knowing about

The original `to_axis()` (in `Gate_Utilization_Process.md`, and presumably
`gate_utilization.py`) only ever *adds* 1440 to push a value into range —
it never reduces one that's already past it. That's fine for an ordinary
claim, but a waypoint-split RON's `gate_out` piece inherits a raw `end`
already past 1440 (RON `end` = next-day-dep + 1440 by construction), so
**both** endpoints land past the chart's right edge and the piece silently
renders off-canvas — a real gate hold that just doesn't show up, no error.

This repo's `index.html` uses the corrected version:
```js
function toAxis(t) {
  return (((t - 180) % 1440) + 1440) % 1440;
}
```
If `gate_utilization.py` still has the old while-loop version, any hub chart
where the waypoint pattern was applied to an RON claim (ISP, BNA, PIE, HOU
per project history) is worth a second look.

## Claim JSON schema

Each entry in a city's `claims` array:

```
start, end       raw minutes-since-03:00-window, pre-axis-transform
label, fleet, kind, row, rowType     unchanged from the original design
arrivalCity       city the aircraft arrived from, or null if genuinely unknown
                  (originate_only has no arrivalCity)
departureCity     city the aircraft departs to, or null if genuinely unknown
                  (terminate_only has no departureCity)
moveTo            {row, rowType} of the NEXT piece in the same waypoint split,
                  only present on a piece that hands off to a different
                  gate/stand a moment later (e.g. gate_in -> stand_mid)
moveFrom          {row, rowType} of the PREVIOUS piece, same idea in reverse
```

`arrivalCity`/`departureCity`/`moveTo`/`moveFrom` are all new. **Backward
compatible by design**: a claim from an older export simply won't have
`arrivalCity`/`departureCity` keys at all (not even as `null`), and the
renderer checks for that (`'arrivalCity' in claim`) to fall back to the
old plain-kind tooltip ("Turn", "Overnight (RON)", ...) with no corner
annotations. No version flag needed in the data — old and new JSON files
can sit side by side in `data/` and both render correctly.

**Tooltip second line**, built from the fields above:
- Plain claim, both sides known: `{arrivalCity} • Turn • {departureCity}`
  or `{arrivalCity} • RON • {departureCity}`
- Only one side known (terminate_only / originate_only): city + RON, only
  2 parts — `PHF • RON` or `RON • PHF`
- Waypoint-split piece: whichever side has a moveTo/moveFrom shows the
  gate/stand code instead of a city (`S1`, `G4`) — e.g. `JAX • RON • S1`
- A `turn`-kind piece that's part of a stand-involving split reads **ROD**
  instead of **Turn** (distinguishes a long same-day park from a genuine
  quick turnaround) — e.g. `S2 • ROD • BHM`

Same fields drive the small corner labels on the chart itself: a piece
with `moveTo` gets the destination code in its bottom-right corner, a
piece with `moveFrom` gets the origin code in its top-left corner.

## Verification

Gate/stand overflow is computed as data (`peakUsed` per city, in the JSON)
and shown as a badge in the UI — no visual chart review needed to catch a
capacity violation. Every claim's on-screen geometry was cross-checked
programmatically (segment widths sum to the claim's true duration, no
segment falls outside the visible axis) rather than verified by eye.
