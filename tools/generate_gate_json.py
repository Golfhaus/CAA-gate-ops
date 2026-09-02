"""
Gate Utilization -- JSON export
================================
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

Run this in the environment that has the real finalized schedule (master_df
with Line, Day, Origin, Dest, Dep, Arr, Fleet) and the current
city_information.csv. This file's __main__ block currently runs against a
labeled SYNTHETIC demo dataset -- swap `load_real_schedule()` in for
`load_demo_schedule()` when pointed at an actual schedule build.
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


# ---------------------------------------------------------------------------
# SYNTHETIC demo dataset -- NOT real Schedule 6 data.
# Real generation needs the master_df (Line/Day/Origin/Dest/Dep/Arr/Fleet)
# from the live schedule-build environment, which this project session
# doesn't have (only the flattened timetable JSON is here, which drops
# Line/Day and can't be re-sequenced into turns/RONs). This demo exists only
# to prove the pipeline + prototype the renderer for a hub, a focus city,
# and a small spoke.
# ---------------------------------------------------------------------------

def load_demo_schedule():
    flights = []
    route_assignments = {}

    def add(line, day, dep, arr, o, d, fleet, route):
        key = (line, day)
        if key in route_assignments and route_assignments[key] != route:
            raise ValueError(f'Route collision on {key}: one Line/Day pair '
                              f'must fly a single continuous route, like the real schedule.')
        flights.append({'line': line, 'day': day, 'dep': dep, 'arr': arr,
                         'origin': o, 'dest': d, 'fleet': fleet})
        route_assignments[key] = route

    # PHF (hub, 16g/8s) -- one line per rotation, single route per line/day,
    # matching how the real schedule's Line/Day structure works.
    add('A', 1, 300, 400, 'PHF', 'JAX', 'MAX9', 701)
    add('A', 1, 430, 530, 'JAX', 'PHF', 'MAX9', 701)
    add('B', 1, 560, 660, 'PHF', 'ORF', 'CRJ900', 501)
    add('B', 1, 690, 790, 'ORF', 'PHF', 'CRJ900', 501)
    add('C', 1, 800, 900, 'PHF', 'RDU', 'CRJ700', 301)
    add('C', 1, 950, 1050, 'RDU', 'PHF', 'CRJ700', 301)
    add('D', 1, 700, 760, 'PHF', 'ILM', 'CRJ200', 101)   # feeds ILM demo below
    add('D', 1, 820, 880, 'ILM', 'PHF', 'CRJ200', 101)
    for i, letter in enumerate('EFGHIJ'):
        t = 500 + i * 90
        add(letter, 1, t, t + 60, 'PHF', f'ZZ{i}', 'CRJ700', 320 + i)
        add(letter, 1, t + 80, t + 140, f'ZZ{i}', 'PHF', 'CRJ700', 320 + i)
    add('K', 1, 1380, 1440, 'PHF', 'ATL', 'MAX9', 705)   # late-departing -> RON overnight
    add('T', 1, 60, 120, 'MOB', 'PHF', 'CRJ200', 110)    # arrives, never leaves again -> terminate_only

    # A short midday PHF turn (route 101 continuation) plus 5 more round-trip
    # lines, to push PHF's peak concurrent claims above its 16-gate cap and
    # exercise the stand-overflow + waypoint-split path (Pass 2), not just
    # the simple case.
    add('N', 1, 600, 660, 'PHF', 'CAE', 'CRJ200', 108)
    add('N', 1, 700, 760, 'CAE', 'PHF', 'CRJ200', 108)
    add('N', 1, 820, 880, 'PHF', 'SAV', 'CRJ200', 108)
    add('N', 1, 920, 980, 'SAV', 'PHF', 'CRJ200', 108)
    for i, letter in enumerate('OPQRS'):
        t = 540 + i * 70
        add(letter, 1, t, t + 55, 'PHF', f'YY{i}', 'CRJ200', 340 + i)
        add(letter, 1, t + 75, t + 130, f'YY{i}', 'PHF', 'CRJ200', 340 + i)

    # BHM (focus city, 6g/4s)
    add('L', 1, 340, 420, 'BHM', 'JAX', 'CRJ700', 330)
    add('L', 1, 450, 530, 'JAX', 'BHM', 'CRJ700', 330)
    add('M', 1, 900, 1420, 'BHM', 'DAY', 'CRJ900', 510)  # RON candidate
    add('M', 2, 350, 900, 'DAY', 'BHM', 'CRJ900', 511)

    # ZZZ -- a minimal, deterministic 1-gate/1-stand city built specifically
    # to force exactly one clean waypoint split, for testing the movement
    # annotations/tooltip logic without depending on PHF's emergent timing.
    add('X', 1, 200, 800, 'BBB', 'ZZZ', 'CRJ200', 601)   # long RON, fully occupies the one gate
    add('X', 2, 350, 900, 'ZZZ', 'BBB', 'CRJ200', 602)
    add('Y', 1, 1000, 1050, 'CCC', 'ZZZ', 'CRJ700', 603)  # short same-day turn, forces the split
    add('Y', 1, 1090, 1140, 'ZZZ', 'DDD', 'CRJ700', 603)

    # WWW -- same idea, but forces a split on a long SAME-DAY 'turn' claim
    # (not an overnight RON), to test the ROD tooltip word.
    add('U', 1, 40, 100, 'EEE', 'WWW', 'CRJ200', 604)
    add('U', 1, 1100, 1150, 'WWW', 'EEE', 'CRJ200', 604)
    add('V', 1, 500, 550, 'FFF', 'WWW', 'CRJ700', 605)   # short mid-day turn, forces the split
    add('V', 1, 590, 640, 'WWW', 'GGG', 'CRJ700', 605)

    city_meta = {
        'PHF': {'name': 'Newport News VA', 'isHub': True, 'isFocusCity': False, 'nGates': 16, 'nStands': 8},
        'BHM': {'name': 'Birmingham AL', 'isHub': False, 'isFocusCity': True, 'nGates': 6, 'nStands': 4},
        'ILM': {'name': 'Wilmington NC', 'isHub': False, 'isFocusCity': False, 'nGates': 2, 'nStands': 1},
        'ZZZ': {'name': 'Test City (RON split)', 'isHub': False, 'isFocusCity': False, 'nGates': 1, 'nStands': 1},
        'WWW': {'name': 'Test City (turn/ROD split)', 'isHub': False, 'isFocusCity': False, 'nGates': 1, 'nStands': 1},
    }
    return flights, route_assignments, city_meta


def load_real_schedule(master_df_path='master_df.pkl', city_info_path=None):
    """Fill in against the live Schedule 6 build environment. Expected shape:

    all_flights: list of dicts, one per leg -- {'line','day','dep','arr',
        'origin','dest','fleet'}. dep/arr as raw minutes-since-midnight ints
        (same convention master_df already uses -- see Timetable_App_Process.md
        Step 3, which reads this same master_df and calls fmt_time() to turn
        these into "HH:MM" for the timetable JSON. Don't call fmt_time() here;
        build_claims() wants the raw ints).

    route_assignments: {(line, day): route_number}. Confirm the source column
        against Build Instructions Sec 2.8 / Sec 2.5's fleet-blocked scheme
        (CRJ200 101+, CRJ700 301+, CRJ900 501+, MAX9 701+) -- pull from
        master_df directly if it already carries a Route column, don't
        recompute the numbering scheme here.

    city_meta: {code: {'name','isHub','isFocusCity','nGates','nStands'}} for
        EVERY active city (use city_information.csv's Active=='Y'), not just
        hubs -- that's the whole point of this rewrite. nGates/nStands come
        from Gate_Allocation_Override / Stand_Allocation_Override where
        present. OPEN QUESTION, resolve before running for real: what's the
        default gate/stand count for a SPOKE with no override? The process
        doc only documents the hub default (16/8) -- confirm the spoke
        default in this chat's context (Build Instructions Sec 2.2 or
        wherever it actually lives) rather than assuming 16/8 applies to a
        2-gate city too.
    """
    import pandas as pd, sys
    sys.path.insert(0, '/home/claude/caa_build')  # or wherever this chat keeps it
    import pk_paths
    df = pd.read_pickle(master_df_path)
    city_info = pd.read_csv(city_info_path or pk_paths.resolve('city_information', '.csv'))
    raise NotImplementedError('fill in against the real master_df in this chat')


if __name__ == '__main__':
    flights, routes, city_meta = load_demo_schedule()  # swap to load_real_schedule() for a real run
    export_schedule(flights, routes, city_meta,
                     label='DEMO -- synthetic data, not Schedule 6',
                     sched_id='demo')
    write_manifest([('demo', 'Demo (synthetic sample)', 'gate_demo.json')])
    print('Wrote gate_demo.json + gate_manifest.json')
