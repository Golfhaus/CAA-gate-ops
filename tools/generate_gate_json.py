"""
Gate Utilization -- JSON export (CORE ENGINE -- this is the file that goes to PK)
==================================================================================
Companion to Gate_Utilization_Process.md. Reuses that document's exact,
already-validated build_claims() / assign_gates() logic (interval-graph
coloring, cyclical overlap checks, the targeted waypoint-split repair).

The ONLY change from the existing process is what happens after gate
assignment: instead of Step 3 (matplotlib render -> PNG), this serializes
the claim list + gate assignment straight to JSON, per city, for the
gate-ops web app to render client-side.

Because build_claims()/assign_gates() already take CITY as a parameter and
don't special-case hubs, looping this over every active city costs nothing
extra algorithmically -- it's the matplotlib render + visual review that
made hub-only coverage a rational cost tradeoff before, not the underlying
computation. Removing the render removes that constraint.

THIS FILE IS DELIBERATELY A PURE LIBRARY. It has no __main__ block and no
demo/test data of any kind -- that's the whole point. Whatever schedule
chat wants real gate-ops JSON should write its OWN small loader script,
living in its OWN environment, that imports this module and calls
export_schedule(). Keep that loader OUT of this file: if it lived here,
every time this file gets re-uploaded from the gate-ops chat (a new
fleet color, a tooltip fix, whatever), it would silently overwrite that
loader along with it -- which is exactly what happened once already
(see the gate-ops chat's session notes on the incident this restructuring
is a response to).

export_schedule() needs three things prepared by the caller:
  all_flights:       list of {'line','day','dep','arr','origin','dest','fleet'}
                      dep/arr as raw minutes-since-midnight ints (same
                      convention master_df already uses for Timetable_App_
                      Process.md's Step 3 -- don't run those through
                      fmt_time() first, build_claims() wants the raw ints).
  route_assignments: {(line, day): route_number}, pulled from whatever
                      column already carries this per Build Instructions
                      Sec 2.8 / Sec 2.5's fleet-blocked scheme -- don't
                      recompute the numbering here.
  city_meta:         {code: {'name','isHub','isFocusCity','nGates','nStands'}}
                      for EVERY active city (city_information.csv's
                      Active=='Y'), not just hubs. nGates/nStands from the
                      Gate_/Stand_Allocation_Override columns where present;
                      confirm the non-hub default against Build Instructions
                      Sec 2.2 rather than assuming the hub default (16/8)
                      applies to a small spoke too.
"""

import json
from collections import defaultdict

FLEET_COLORS = {
    'MAX9':   '#e3b3a3',   # soft terracotta -- echoes the coral accent, muted
    'CRJ900': '#aec4dc',   # soft powder blue -- echoes navy, muted
    'CRJ700': '#a9cdb2',   # soft sage -- cool complement, keeps 4 fleets distinct
    'CRJ200': '#e8cd93',   # soft honey/sand -- warm, echoes the cream background
}
# All four sit around L~80-85%/muted saturation specifically so black claim-
# label text stays comfortably readable -- contrast ratio against #1a1a1a is
# 9.3:1 to 11.3:1 for all four (WCAG AAA is 7:1); the old palette's MAX9 was
# only 4.8:1 (fails AAA, barely clears AA-large).

TOUCH_ARR = 45
TOUCH_DEP = 60
SHORT_THRESHOLD = 150


# ---------------------------------------------------------------------------
# Unchanged from Gate_Utilization_Process.md
# ---------------------------------------------------------------------------

def build_claims(all_flights, route_assignments, CITY):
    """Same interval logic as before, now also carrying the arrival-from and
    departure-to city for each claim (needed for the richer tooltip). A city
    is None when that side is genuinely unknown -- terminate_only has no
    departure_city, originate_only has no arrival_city -- never guessed."""
    lines = defaultdict(dict)
    for f in all_flights:
        lines[f['line']].setdefault(f['day'], []).append(f)
    for L in lines:
        for D in lines[L]:
            lines[L][D].sort(key=lambda x: x['dep'])

    claims = []
    for L, days in lines.items():
        max_day = max(days.keys())
        for D in sorted(days.keys()):
            legs = days[D]
            route_this = route_assignments[(L, D)]
            fleet = legs[0]['fleet']
            for i, leg in enumerate(legs):
                if leg['dest'] == CITY:
                    nxt = None
                    for j in range(i + 1, len(legs)):
                        if legs[j]['origin'] == CITY:
                            nxt = legs[j]
                            break
                    if nxt:
                        claims.append((leg['arr'], nxt['dep'], str(route_this), fleet, 'turn',
                                        leg['origin'], nxt['dest']))
                    else:
                        nD = D + 1 if D + 1 <= max_day else 1
                        nxt_day_legs = days.get(nD, [])
                        if nxt_day_legs and nxt_day_legs[0]['origin'] == CITY:
                            route_next = route_assignments[(L, nD)]
                            claims.append((leg['arr'], nxt_day_legs[0]['dep'] + 1440,
                                           f"{route_this} -> {route_next}", fleet, 'ron',
                                           leg['origin'], nxt_day_legs[0]['dest']))
                        else:
                            claims.append((leg['arr'], leg['arr'] + 45, str(route_this), fleet, 'terminate_only',
                                            leg['origin'], None))
            first_leg = legs[0]
            if first_leg['origin'] == CITY:
                pD = D - 1 if D > 1 else max_day
                prev_day_legs = days.get(pD, [])
                already = prev_day_legs and prev_day_legs[-1]['dest'] == CITY
                if not already:
                    claims.append((first_leg['dep'] - 60, first_leg['dep'], str(route_this), fleet, 'originate_only',
                                    None, first_leg['dest']))
    return claims


def overlaps(c1, c2):
    s1, e1 = c1[0], c1[1]
    s2, e2 = c2[0], c2[1]
    for sh1 in (-1440, 0, 1440):
        for sh2 in (-1440, 0, 1440):
            if s1 + sh1 < e2 + sh2 and s2 + sh2 < e1 + sh1:
                return True
    return False


def split_for_waypoint(claim):
    start, end, label, fleet, kind, arrival_city, departure_city = claim
    # All three pieces carry the same true arrival/departure city -- the
    # renderer decides which side to actually display based on whether a
    # piece has a moveTo/moveFrom neighbor (see city_claims_to_json).
    gate_in = (start, start + TOUCH_ARR, label, fleet, kind, arrival_city, departure_city)
    stand_mid = (start + TOUCH_ARR, end - TOUCH_DEP, label, fleet, kind, arrival_city, departure_city)
    gate_out = (end - TOUCH_DEP, end, label, fleet, kind, arrival_city, departure_city)
    return gate_in, stand_mid, gate_out


def assign_gates(claims, n_gates=None):
    claims_sorted = sorted(claims, key=lambda c: c[0])
    gates = defaultdict(list)
    assignment = {}
    for c in claims_sorted:
        gate_num = 1
        while not all(not overlaps(c, e) for e in gates[gate_num]):
            gate_num += 1
        gates[gate_num].append(c)
        assignment[c] = gate_num

    if n_gates is None:
        return [(c, g) for c, g in assignment.items()]

    overflowed_short = [c for c in claims if assignment[c] > n_gates
                         and (c[1] - c[0]) <= SHORT_THRESHOLD]

    for short_c in overflowed_short:
        best_gate, best_blocker = None, None
        for g in range(1, n_gates + 1):
            blockers = [e for e in gates[g] if overlaps(short_c, e)]
            if len(blockers) == 1 and (blockers[0][1] - blockers[0][0]) > SHORT_THRESHOLD * 2:
                if best_blocker is None or (blockers[0][1] - blockers[0][0]) > (best_blocker[1] - best_blocker[0]):
                    best_gate, best_blocker = g, blockers[0]
        if best_gate is None:
            continue

        gates[best_gate].remove(best_blocker)
        del assignment[best_blocker]
        gate_in, stand_mid, gate_out = split_for_waypoint(best_blocker)
        for piece in (gate_in, gate_out):
            gates[best_gate].append(piece)
            assignment[piece] = best_gate
        old_gate = assignment[short_c]
        if short_c in gates[old_gate]:
            gates[old_gate].remove(short_c)
        gates[best_gate].append(short_c)
        assignment[short_c] = best_gate
        stand_num = n_gates + 1
        while any(overlaps(stand_mid, e) for e in gates[stand_num]):
            stand_num += 1
        gates[stand_num].append(stand_mid)
        assignment[stand_mid] = stand_num

    return [(c, g) for c, g in assignment.items()]


# ---------------------------------------------------------------------------
# New: serialize to the gate-ops app's JSON schema instead of rendering
# ---------------------------------------------------------------------------

def city_claims_to_json(claims_with_gate, n_gates):
    """(claim, gate_num) pairs -> list of JSON-ready claim dicts. Raw minute
    values are kept as-is; the 03:00-03:00 axis transform + wraparound
    segment-splitting are ported to the renderer (mirrors how Step 3 of the
    existing process does it) rather than baked into the data.

    Also detects waypoint-split siblings (same label, time-contiguous,
    different row) and records each piece's next/previous physical location
    as moveTo/moveFrom -- e.g. the gate_in piece of a split gets
    moveTo={'row':2,'rowType':'stand'} because it hands off to stand 2 next.
    The renderer uses this for the corner annotations and for picking which
    side of the tooltip shows a real city vs. a gate/stand code."""
    out = []
    for (start, end, label, fleet, kind, arrival_city, departure_city), gate_num in claims_with_gate:
        row_type = 'gate' if gate_num <= n_gates else 'stand'
        row_num = gate_num if gate_num <= n_gates else gate_num - n_gates
        out.append({
            'start': start, 'end': end, 'label': label, 'fleet': fleet,
            'kind': kind, 'row': row_num, 'rowType': row_type,
            'arrivalCity': arrival_city, 'departureCity': departure_city,
        })
    out.sort(key=lambda c: c['start'])

    by_label = defaultdict(list)
    for c in out:
        by_label[c['label']].append(c)
    for label, pieces in by_label.items():
        if len(pieces) < 2:
            continue
        pieces_sorted = sorted(pieces, key=lambda c: c['start'])
        for i in range(len(pieces_sorted) - 1):
            a, b = pieces_sorted[i], pieces_sorted[i + 1]
            if a['end'] == b['start'] and (a['row'], a['rowType']) != (b['row'], b['rowType']):
                a['moveTo'] = {'row': b['row'], 'rowType': b['rowType']}
                b['moveFrom'] = {'row': a['row'], 'rowType': a['rowType']}

    # displayLabel: what actually renders in the block. Usually identical to
    # `label`. Exception is a waypoint-split RON: only ONE of its pieces is
    # "where the aircraft sits at 03:00" -- the piece whose [start,end) span
    # actually contains the 03:00 axis reference instant (the same test that
    # decides whether a piece renders as one segment or wraps into two).
    # That piece keeps the full "routeThis -> routeNext" label; every other
    # piece in the same split gets no center label at all -- the corner
    # moveTo/moveFrom code already says where it's headed. This is about
    # which piece spans 03:00, not about gate vs. stand -- a split RON is
    # virtually always long enough that its 03:00 instant falls in the
    # long middle piece, but the test itself doesn't assume that.
    # (turn/ROD splits have no dual-route concept -- one route number,
    # unaffected, shown on every piece as before.)
    def to_axis(t):
        return (t - 180) % 1440

    def spans_0300(start, end):
        x0, x1 = to_axis(start), to_axis(end)
        if x1 <= x0:
            x1 += 1440
        return x1 > 1440

    for c in out:
        c['displayLabel'] = c['label']

    for label, pieces in by_label.items():
        if len(pieces) < 2 or pieces[0]['kind'] != 'ron':
            continue
        spanning = [p for p in pieces if spans_0300(p['start'], p['end'])]
        if not spanning:
            # Safety net for an unusual RON whose split pieces somehow never
            # cross 03:00 -- keep the longest piece labeled rather than
            # blanking every piece and losing the information entirely.
            spanning = [max(pieces, key=lambda p: p['end'] - p['start'])]
        for p in pieces:
            p['displayLabel'] = p['label'] if p in spanning else ''
    return out


def export_schedule(all_flights, route_assignments, city_meta, label, sched_id):
    """city_meta: {code: {'name':..., 'isHub':bool, 'isFocusCity':bool,
    'nGates':int, 'nStands':int}} for every ACTIVE city -- not just hubs."""
    cities_out = []
    for code, meta in city_meta.items():
        claims = build_claims(all_flights, route_assignments, code)
        assignment = assign_gates(claims, n_gates=meta['nGates'])
        cities_out.append({
            'code': code, 'name': meta['name'],
            'isHub': meta['isHub'], 'isFocusCity': meta['isFocusCity'],
            'nGates': meta['nGates'], 'nStands': meta['nStands'],
            'peakUsed': max((sum(1 for c in claims if overlaps(c, (t, t + 1, '', '', '')))
                              for t in range(0, 1440, 15)), default=0),
            'claims': city_claims_to_json(assignment, meta['nGates']),
        })
    schedule_json = {
        'label': label, 'fleetColors': FLEET_COLORS, 'cities': cities_out,
    }
    with open(f'gate_{sched_id}.json', 'w') as f:
        json.dump(schedule_json, f, indent=1)
    return schedule_json


def write_manifest(entries, path='gate_manifest.json'):
    """entries: list of (id, label, filename), newest first."""
    manifest = [{'id': i, 'label': l, 'file': fn} for i, l, fn in entries]
    with open(path, 'w') as f:
        json.dump(manifest, f, indent=1)
