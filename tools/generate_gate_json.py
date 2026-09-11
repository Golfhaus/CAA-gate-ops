"""
Gate Utilization -- JSON export
================================
Companion to Gate_Utilization_Process.md. Reuses that document's
build_claims() logic (interval-graph coloring, cyclical overlap checks),
extended with arrival/departure city tracking (for richer tooltips) and
moveTo/moveFrom linking of waypoint-split siblings (so the renderer can
show which gate/stand a piece hands off to). assign_gates() is NOT
identical to the process-doc version -- see that function's own docstring.

The ONLY change from the matplotlib process is what happens after gate
assignment: instead of rendering a PNG, this serializes the claim list +
gate assignment straight to JSON, per city, for the gate-ops web app to
render client-side. This is the primary path for gate/stand charts going
forward, replacing per-hub PNG generation (Finalize item A21).

Run against the real finalized schedule (all_flights list with line, day,
origin, dest, dep, arr, fleet per leg) and the current city_information.csv.

STATUS AS OF THIS REBUILD: a version of this file with load_real_schedule()
filled in and the assign_gates() fixes below ported in was built once
already this build, then confirmed (via a direct PK audit) to have never
actually been uploaded -- what shipped instead was an earlier stub. This is
that work redone. If this file is ever found reverted to a stub again,
that's the same failure mode recurring, not a new one -- check upload
status directly rather than assuming a described fix is actually live.
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
# build_claims() -- carries arrival/departure city per claim
# ---------------------------------------------------------------------------

def build_claims(all_flights, route_assignments, CITY):
    """Interval logic for one city's gate/stand claims, carrying the
    arrival-from and departure-to city for each claim (needed for the
    richer tooltip). A city is None when that side is genuinely unknown --
    terminate_only has no departure_city, originate_only has no
    arrival_city -- never guessed.

    The originate_only duplicate-suppression check (further down) must
    match gate_utilization.py's exactly: "was this exact departure already
    covered by a same-day turn claim ending at this time" -- NOT "did the
    previous day end at this city." A prior version of this function used
    the latter (a RON-continuity check, answering a different question
    entirely) and silently produced a different claim count than
    gate_utilization.py for the same city on the same data -- confirmed on
    CAE and EYW, where it suppressed claims gate_utilization.py correctly
    kept, undercounting gate pressure and hiding real turns-on-stands at
    both. Found by diffing the two functions' raw output on identical
    input after their turns-on-stands counts didn't match, not by
    inspection -- the two files' logic had quietly diverged since this one
    was last ported from gate_utilization.py, and nothing had re-compared
    them since.
    """
    lines = defaultdict(dict)
    for f in all_flights:
        lines[f['line']].setdefault(f['day'], []).append(f)
    for L in lines:
        for D in lines[L]:
            lines[L][D].sort(key=lambda x: x['dep'])

    claims = []
    for L, days in lines.items():
        max_day = max(days.keys())
        days_sorted = sorted(days.keys())
        # PASS 1: turn / ron / terminate_only for EVERY day of this line,
        # including the wraparound RON (last day -> day 1). Done as a
        # complete pass over the whole line before PASS 2 (below) runs any
        # originate_only check for it -- see PASS 2's comment for why this
        # ordering is the fix, not just a reshuffle. Sorted-day iteration
        # alone isn't enough: it puts day 1 before day max_day, but the
        # wraparound RON is only ever created while day max_day is being
        # processed, so a check running during day 1's own turn can never
        # see it no matter what order the OTHER claim types are built in.
        for D in days_sorted:
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
                            # TOUCH_ARR, not a bare 45 -- this file's own value happened
                            # to already be semantically correct (terminate_only is
                            # arrival-side), but hardcoded rather than tied to the named
                            # constant, which is what let gate_utilization.py's copy
                            # drift to the WRONG constant (TOUCH_DEP) without either
                            # file's own tests catching it. Fixed there; naming it here
                            # too so the two can't silently diverge again.
                            claims.append((leg['arr'], leg['arr'] + TOUCH_ARR, str(route_this), fleet, 'terminate_only',
                                            leg['origin'], None))
        # PASS 2: originate_only dedup check for every day of this line, now
        # against `claims` after PASS 1 has added this line's COMPLETE set
        # of turn/ron/terminate_only claims -- including the wraparound RON.
        # Previously this check ran interleaved with PASS 1, immediately
        # after each day's own claims were built; for a normal day D-1 ->
        # day D RON that's fine (D-1 was already processed), but the
        # wraparound (max_day -> day 1) RON doesn't exist yet when day 1's
        # check runs, since day 1 is processed FIRST in sorted order and
        # max_day LAST. That produced a spurious originate_only claim
        # duplicating the wraparound RON's own tail -- 13 instances across
        # the delivered v2.2.3 gate JSON (7 at DAY, 6 at JAX), all
        # same-route wraparounds, confirmed via a real DAY screenshot
        # (route 501 shown occupying two gates for one departure) before
        # this fix. Splitting into two passes removes the ordering
        # dependency entirely -- PASS 2 never runs before PASS 1 has
        # finished, for any day of this line, wraparound included.
        for D in days_sorted:
            legs = days[D]
            route_this = route_assignments[(L, D)]
            fleet = legs[0]['fleet']
            first_leg = legs[0]
            if first_leg['origin'] == CITY:
                # Matches EITHER a plain claim for this exact route, OR a RON
                # claim whose tail half is this route (label "prevRoute ->
                # thisRoute") -- a RON's own gate-out piece already represents
                # this exact same first-leg departure, and without the second
                # condition this check can never see that, since a RON's
                # label is never equal to the bare route number no matter
                # which side of the transition it's checking.
                #
                # Compares end time MODULO 1440, not the raw value -- see
                # gate_utilization.py's copy of this comment for the full
                # reasoning. A RON wrapping back to this route ends at
                # first_leg['dep'] + 1440, exactly one day later in raw
                # terms; without the modulo this never matches even after
                # the label fix above, since 1440 is nowhere near the 0.5
                # tolerance. Confirmed on BHM route 103 (line S).
                already = any((c[2] == str(route_this) or c[2].endswith(f" -> {route_this}"))
                               and abs((c[1] - first_leg['dep']) % 1440) < 0.5
                               for c in claims)
                if not already:
                    # TOUCH_DEP, not a bare 60 -- see the terminate_only comment above.
                    claims.append((first_leg['dep'] - TOUCH_DEP, first_leg['dep'], str(route_this), fleet, 'originate_only',
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


def apply_forced_stand_splits(claims, force_labels):
    """
    Same purpose and mechanism as gate_utilization.py's copy -- see that
    file's docstring for the full reasoning (voluntarily converting a
    comfortably-gate-resident long RON into a gate-in/stand-mid/gate-out
    split, with the middle piece marked so assign_gates() sends it
    straight to a stand instead of Pass 1 putting it right back on a
    gate). Adapted here only for this file's 7-tuple claim shape
    (arrival_city/departure_city appended).
    """
    result = []
    for c in claims:
        if c[2] in force_labels:
            gate_in, stand_mid, gate_out = split_for_waypoint(c)
            stand_mid = (stand_mid[0], stand_mid[1], stand_mid[2], stand_mid[3],
                         'forced_stand_mid', stand_mid[5], stand_mid[6])
            result.extend([gate_in, stand_mid, gate_out])
        else:
            result.append(c)
    return result


# ---------------------------------------------------------------------------
# assign_gates() -- ported from gate_utilization.py's latest state, adapted
# to this file's 7-tuple claims (arrival_city/departure_city appended).
# NOT the process-doc version -- three real fixes layered in, each found and
# verified against real Schedule 6 data, not derived from theory alone:
#
#   Pass 2 rescue checks BOTH gate- and stand-resident blockers. The
#   original only ever checked stand-resident candidates when rescuing a
#   short claim stranded on a stand -- confirmed on live BHM data to miss
#   real rescues: a long claim can land on a gate via ordinary first-fit and
#   sit there blocking hours it never needed, while a short claim elsewhere
#   has no path to a rescue sitting right there because the old code never
#   looked at gate-resident claims at all.
#
#   Pass 3 unconditionally splits any long claim still directly on a stand,
#   regardless of whether a rescue was needed -- an aircraft cannot deplane
#   or board on a stand, full stop.
#
#   Pass 4 is a final compaction sweep for pieces stranded on a stand by
#   the first three passes' point-in-time sequential first-fit, even when a
#   gate is genuinely free in the completed assignment. This one shipped
#   two real bugs before landing on its current, safe form: a duration-only
#   version moved genuine parked stand_mid pieces onto gates they never
#   needed; a position-based ("sandwiched between same-label neighbors")
#   version also misfired on a claim that had been split more than once,
#   where a legitimately-brief re-entry touch sits sandwiched too. The safe
#   version tracks every stand_mid piece by EXPLICIT PROVENANCE -- recorded
#   at the exact two points this function creates one -- and Pass 4 excludes
#   precisely that set, nothing inferred after the fact.
#
# Unconditionally splitting every long claim upfront (skipping the targeted
# rescue) was tried and reverted separately: it increases how many gates are
# needed at any given moment, since a split claim always contributes two
# independent touch-events instead of possibly one continuous hold, even
# when nothing needed the room.
# ---------------------------------------------------------------------------

def assign_gates(claims, n_gates=None, return_provenance=False):
    """
    return_provenance=True returns (assignment_list, genuine_stand_mid)
    instead of just assignment_list -- same purpose and same reasoning as
    gate_utilization.py's copy of this parameter: a stand_mid's duration
    has no upper bound tied to SHORT_THRESHOLD, so external callers
    counting "short claims stranded on a stand" as a capacity-problem
    proxy need this exact set to correctly exclude legitimately-parked
    middle pieces, not a duration-based guess. Confirmed on BWI: a RON's
    own legitimate 145-minute parked middle segment was being counted as
    a genuine finding without this.
    """
    claims_sorted = sorted(claims, key=lambda c: (0 if (c[1] - c[0]) <= SHORT_THRESHOLD else 1, c[0]))
    gates = defaultdict(list)
    assignment = {}
    for c in claims_sorted:
        # 'forced_stand_mid' claims start their search AFTER n_gates -- see
        # apply_forced_stand_splits()'s docstring and gate_utilization.py's
        # matching copy of this same change.
        gate_num = (n_gates + 1) if (c[4] == 'forced_stand_mid' and n_gates is not None) else 1
        while not all(not overlaps(c, e) for e in gates[gate_num]):
            gate_num += 1
        gates[gate_num].append(c)
        assignment[c] = gate_num

    if n_gates is None:
        result = [(c, g) for c, g in assignment.items()]
        return (result, set()) if return_provenance else result

    genuine_stand_mid = set()
    # 'forced_stand_mid' claims added here immediately -- same reasoning
    # as gate_utilization.py's matching copy: without this, Pass 3's
    # long_on_stand sweep would treat a forced_stand_mid claim as an
    # ordinary long-on-stand claim and split it again.
    for c in claims_sorted:
        if c[4] == 'forced_stand_mid':
            genuine_stand_mid.add(c)

    def try_rescue(short_c):
        for g in list(gates.keys()):
            if g == assignment[short_c]:
                continue
            blockers = [e for e in gates[g] if overlaps(short_c, e)]
            if len(blockers) != 1:
                continue
            blocker = blockers[0]
            if (blocker[1] - blocker[0]) <= SHORT_THRESHOLD:
                continue
            if blocker in genuine_stand_mid:
                # A stand_mid piece is the deliberately-parked middle of an
                # earlier split -- it can still be long (often far over
                # SHORT_THRESHOLD, especially on the very first split of a
                # long RON), so the duration check above does NOT exclude
                # it, and it sits on a stand slot exactly like any other
                # blocker candidate would. Without this check, try_rescue()
                # will happily select an already-parked stand_mid as a
                # "blocker" and split it AGAIN -- and if the new stand_mid
                # that produces is itself later selected the same way, this
                # recurses. Confirmed on real BNA data: a single RON claim
                # (one aircraft, one overnight stay) fragmented into 11
                # separate pieces across repeated re-splits of its own
                # middle, each carrying the same route-pair label, each
                # looking like an independent event to any renderer or
                # capacity count reading the output -- inflating BNA's
                # apparent peak gate demand with claims that were never
                # real, distinct touch-points at all.
                continue
            gates[g].remove(blocker)
            del assignment[blocker]
            gate_in, stand_mid, gate_out = split_for_waypoint(blocker)
            for piece in (gate_in, gate_out):
                if overlaps(piece, short_c) or any(overlaps(piece, e) for e in gates[g] if e is not short_c):
                    pg = 1
                    while pg <= n_gates and any(overlaps(piece, e) for e in gates[pg]):
                        pg += 1
                    if pg > n_gates:
                        pg = n_gates + 1
                        while any(overlaps(piece, e) for e in gates[pg]):
                            pg += 1
                    gates[pg].append(piece); assignment[piece] = pg
                else:
                    gates[g].append(piece); assignment[piece] = g
            old_gate = assignment[short_c]
            gates[old_gate].remove(short_c)
            gates[g].append(short_c)
            assignment[short_c] = g
            stand_num = n_gates + 1
            while any(overlaps(stand_mid, e) for e in gates[stand_num]):
                stand_num += 1
            gates[stand_num].append(stand_mid)
            assignment[stand_mid] = stand_num
            genuine_stand_mid.add(stand_mid)
            return True
        return False

    overflowed_short = [c for c in claims if assignment[c] > n_gates
                         and (c[1] - c[0]) <= SHORT_THRESHOLD]
    for short_c in overflowed_short:
        try_rescue(short_c)

    # Excludes genuine_stand_mid for the same reason try_rescue() does above:
    # a stand_mid piece can still be long (often far over SHORT_THRESHOLD on
    # the first split of a long RON), and without this exclusion this pass
    # would unconditionally select an already-deliberately-parked stand_mid
    # and split it again. Confirmed live: fixing ONLY try_rescue()'s copy of
    # this bug still left BNA's single RON claim fragmenting into 5 pieces
    # instead of 3, because this pass re-split the same stand_mid a second
    # time immediately afterward, independent of try_rescue() ever running.
    long_on_stand = sorted([c for c in list(assignment.keys())
                      if (c[1] - c[0]) > SHORT_THRESHOLD and assignment[c] > n_gates
                      and c not in genuine_stand_mid],
                      key=lambda c: c[0])
    for long_c in long_on_stand:
        old_slot = assignment[long_c]
        gates[old_slot].remove(long_c)
        del assignment[long_c]
        gate_in, stand_mid2, gate_out = split_for_waypoint(long_c)
        for piece in (gate_in, gate_out):
            pg = 1
            while pg <= n_gates and any(overlaps(piece, e) for e in gates[pg]):
                pg += 1
            if pg > n_gates:
                pg = n_gates + 1
                while any(overlaps(piece, e) for e in gates[pg]):
                    pg += 1
            gates[pg].append(piece); assignment[piece] = pg
        stand_num2 = n_gates + 1
        while any(overlaps(stand_mid2, e) for e in gates[stand_num2]):
            stand_num2 += 1
        gates[stand_num2].append(stand_mid2)
        assignment[stand_mid2] = stand_num2
        genuine_stand_mid.add(stand_mid2)

    stand_resident_short = [c for c in list(assignment.keys())
                             if assignment[c] > n_gates and (c[1] - c[0]) <= SHORT_THRESHOLD
                             and c not in genuine_stand_mid]
    for c in sorted(stand_resident_short, key=lambda c: c[0]):
        current_slot = assignment[c]
        for g in range(1, n_gates + 1):
            if not any(overlaps(c, e) for e in gates[g]):
                gates[current_slot].remove(c)
                gates[g].append(c)
                assignment[c] = g
                break

    result = [(c, g) for c, g in assignment.items()]
    return (result, genuine_stand_mid) if return_provenance else result


# ---------------------------------------------------------------------------
# Serialize to the gate-ops app's JSON schema, with moveTo/moveFrom
# ---------------------------------------------------------------------------

def city_claims_to_json(claims_with_gate, n_gates):
    """(claim, gate_num) pairs -> list of JSON-ready claim dicts. Raw minute
    values are kept as-is; the 03:00-03:00 axis transform + wraparound
    segment-splitting are ported to the renderer rather than baked into
    the data.

    Also detects waypoint-split siblings (same label, time-contiguous,
    different row) and records each piece's next/previous physical
    location as moveTo/moveFrom -- e.g. the gate_in piece of a split gets
    moveTo={'row':2,'rowType':'stand'} because it hands off to stand 2
    next. The renderer uses this for corner annotations and for picking
    which side of the tooltip shows a real city vs. a gate/stand code."""
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
    return out


def export_schedule(all_flights, route_assignments, city_meta, label, sched_id, out_dir='.',
                     force_stand_splits=None):
    """city_meta: {code: {'name':..., 'isHub':bool, 'isFocusCity':bool,
    'nGates':int, 'nStands':int}} for every ACTIVE city -- not just hubs.

    force_stand_splits: optional {city_code: set_of_labels} -- for each
    named city, the given claim labels are run through
    apply_forced_stand_splits() before gate assignment. See that
    function's own docstring, and try_gate_to_stand_relief() in
    caa_extension_tools.py, for how a candidate is identified and
    confirmed before being named here. This is a pure ground-handling
    choice (which physical spot an aircraft's own idle time uses), not a
    schedule change -- it never touches all_flights.
    """
    force_stand_splits = force_stand_splits or {}
    cities_out = []
    for code, meta in city_meta.items():
        claims = build_claims(all_flights, route_assignments, code)
        if code in force_stand_splits:
            claims = apply_forced_stand_splits(claims, force_stand_splits[code])
        assignment = assign_gates(claims, n_gates=meta['nGates'])
        cities_out.append({
            'code': code, 'name': meta['name'],
            'isHub': meta['isHub'], 'isFocusCity': meta['isFocusCity'],
            'nGates': meta['nGates'], 'nStands': meta['nStands'],
            'peakUsed': max((sum(1 for c in claims if overlaps(c, (t, t + 1, '', '', '', None, None)))
                              for t in range(0, 1440, 15)), default=0),
            'claims': city_claims_to_json(assignment, meta['nGates']),
        })
    schedule_json = {
        'label': label, 'fleetColors': FLEET_COLORS, 'cities': cities_out,
    }
    out_path = f'{out_dir}/gate_{sched_id}.json'
    with open(out_path, 'w') as f:
        json.dump(schedule_json, f, indent=1)
    return schedule_json


def write_manifest(entries, path='gate_manifest.json'):
    """entries: list of (id, label, filename), newest first."""
    manifest = [{'id': i, 'label': l, 'file': fn} for i, l, fn in entries]
    with open(path, 'w') as f:
        json.dump(manifest, f, indent=1)


# ---------------------------------------------------------------------------
# Real Schedule 6 loader
# ---------------------------------------------------------------------------

def load_real_schedule(flights_pkl='/tmp/all_flights.pkl', city_info_csv=None):
    """
    Loads the real, current Schedule 6 flight data and builds city_meta for
    every active city.

    Gate/stand defaults resolved from Build Instructions Sec 2.2 (previously
    an open question in this file's own docstring, now confirmed):

        Hub               16 gates / 8 stands
        Focus city (BHM)   6 gates / 4 stands
        Destination         2 gates / 2 stands

    Gate_Allocation_Override / Stand_Allocation_Override in
    city_information.csv take precedence per-field when populated (a city
    can override just one of the two -- e.g. ABE has a gate override but no
    stand override, confirmed -- falling back to its status default for the
    unpopulated field only).
    """
    import pickle
    import pandas as pd
    import sys
    sys.path.insert(0, '/home/claude/caa_build')
    import pk_paths

    with open(flights_pkl, 'rb') as f:
        flights = pickle.load(f)

    FLEET_BLOCKS = {'CRJ200': 101, 'CRJ700': 301, 'CRJ900': 501, 'MAX9': 701}
    route_num = {}
    counters = dict(FLEET_BLOCKS)
    for f in sorted(flights, key=lambda f: (f['fleet'], f['line'], f['day'], f['dep'])):
        key = (f['fleet'], f['line'], f['day'])
        if key not in route_num:
            route_num[key] = counters[f['fleet']]
            counters[f['fleet']] += 1
    route_assignments = {(f['line'], f['day']): str(route_num[(f['fleet'], f['line'], f['day'])])
                          for f in flights}

    STATUS_DEFAULTS = {
        'Hub': (16, 8),
        'Focus City': (6, 4),
        'Destination': (2, 2),
    }

    city_info_path = city_info_csv or pk_paths.resolve('city_information', '.csv')
    city_info = pd.read_csv(city_info_path, encoding='utf-8-sig')
    city_info = city_info[city_info['Active'] == 'Y']

    def display_name(row):
        if pd.notna(row.get('Long_City')) and str(row['Long_City']).strip():
            return str(row['Long_City']).strip()
        return str(row['City']).strip()

    city_meta = {}
    for _, row in city_info.iterrows():
        code = row['Code']
        status = row['Status']
        default_g, default_s = STATUS_DEFAULTS.get(status, (2, 2))
        g_override = row.get('Gate_Allocation_Override')
        s_override = row.get('Stand_Allocation_Override')
        n_gates = int(g_override) if pd.notna(g_override) else default_g
        n_stands = int(s_override) if pd.notna(s_override) else default_s
        city_meta[code] = {
            'name': display_name(row),
            'isHub': status == 'Hub',
            'isFocusCity': status == 'Focus City',
            'nGates': n_gates,
            'nStands': n_stands,
        }

    return flights, route_assignments, city_meta


if __name__ == '__main__':
    flights, routes, city_meta = load_real_schedule()
    print(f"Loaded {len(flights)} flights, {len(city_meta)} active cities")
    export_schedule(flights, routes, city_meta,
                     label='Schedule 6 (current build)',
                     sched_id='sked6_current')
    write_manifest([('sked6_current', 'Schedule 6 (current build)', 'gate_sked6_current.json')])
    print('Wrote gate_sked6_current.json + gate_manifest.json')
