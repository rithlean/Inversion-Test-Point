# -*- coding: utf-8 -*-
# =============================================================================
# ITP Selection Script  --  Control Points (CP) + Observation Points (OP)
# =============================================================================
# Usage:
#   python itp_selection.py [circuit1 circuit2 ...]
#
# Examples:
#   python itp_selection.py                  # process b14, b15, b17
#   python itp_selection.py b15              # process b15 only
#   python itp_selection.py b14 b17          # process b14 and b17
#
# Fault file naming convention (must match):
#   b14 --> b14_ND_verbose.rpt
#   b15 --> b15_ND_verbose.rpt
#   b17 --> b17_ND_verbose.rpt
#
# Outputs per circuit (example for b15, top-15):
#   b15_cp_top15.csv          Control point candidates (XOR insertion)
#   b15_op_top15.csv          Observation point candidates (scan tap insertion)
#   b15_combined_top15.csv    Mixed CP+OP ranked list
#   itp_selection_summary.csv Master summary across all circuits
#
# Selection logic:
#   CP candidates : NC faults on /Y output pins
#                   Hard to control (high CC of needed value) AND observable
#                   Score = 0.4*ctrl_difficulty + 0.4*CO + 0.2*CC_diff
#                   One candidate per unique hierarchy block (diversity enforced)
#
#   OP candidates : NO faults on /Y or /Q or /QN output pins
#                   Hard to observe (high CO) AND reasonably easy to reach
#                   Score = 0.6*CO + 0.4*min(CC0,CC1)
#                   One candidate per unique hierarchy block
# =============================================================================

import re
import csv
import sys
import os

# ---------------------------------------------------------------------------
# Thresholds -- tune these if you want more/fewer candidates
# ---------------------------------------------------------------------------
CP_MIN_CTRL   = 5     # Minimum controllability difficulty to be a CP candidate
CP_MIN_CO     = 3     # Minimum CO value for CP (we still want it observable)
OP_MIN_CO     = 20    # Minimum CO to be an OP candidate (hard to observe)
OP_MAX_CTRL   = 200   # Maximum CC (easier to reach = better OP target)
MAX_PER_HIER  = 1     # Max candidates from the same hierarchy block


# ---------------------------------------------------------------------------
# Parse the TetraMAX verbose fault report
# Returns two lists: cp_candidates, op_candidates
# ---------------------------------------------------------------------------
def parse_verbose_faults(filepath):
    cp_candidates = []
    op_candidates = []
    seen_nodes    = set()

    with open(filepath) as f:
        for line in f:
            line = line.strip()

            if 'SCOAP=' not in line:
                continue

            parts = line.split()
            if len(parts) < 3:
                continue
            if parts[0] not in ('sa0', 'sa1'):
                continue

            fault_type  = parts[0]   # sa0 or sa1
            fault_class = parts[1]   # NC, NO, --, UD etc.
            node        = parts[2]

            # Only NC (not-covered) for CP, NO (not-observed) for OP
            if fault_class not in ('NC', 'NO'):
                continue

            # Extract cell type from (CELLNAME)
            cell_m = re.search(r'\(([A-Z][^)]+)\)', line)
            if not cell_m:
                continue
            cell = cell_m.group(1)

            # Extract SCOAP values  SCOAP=CC0/CC1/CO
            scoap_m = re.search(r'SCOAP=(\d+)/(\d+)/(\d+)', line)
            if not scoap_m:
                continue

            CC0 = int(scoap_m.group(1))
            CC1 = int(scoap_m.group(2))
            CO  = int(scoap_m.group(3))

            # Skip reset/set pins -- they are not useful insertion points
            if any(x in node for x in ['RSTB', 'RST', 'SETB', 'reset', 'set']):
                continue

            # Skip duplicates
            key = (node, fault_type)
            if key in seen_nodes:
                continue
            seen_nodes.add(key)

            # Derive hierarchy prefix for diversity enforcement
            # e.g.  \DP_OP_534J1_133_2776/U27/Y  -->  \DP_OP_534J1_133_2776
            #        U4097/Y                       -->  top
            node_parts = node.split('/')
            if len(node_parts) >= 3:
                hier = node_parts[0]           # deep hierarchy block
            elif len(node_parts) == 2:
                hier = node_parts[0]           # e.g. U4097 -- flat top-level
            else:
                hier = 'top'

            pin = node_parts[-1] if len(node_parts) > 1 else node

            # ------------------------------------------------------------------
            # CP selection: NC faults on /Y output pins only
            # We want nodes that are hard to control (need forcing) AND
            # are observable enough that a CP there will help fault propagation
            # ------------------------------------------------------------------
            if fault_class == 'NC' and pin == 'Y':
                # ctrl_difficulty = how hard it is to set the node to the
                # required value (opposite of the stuck fault)
                if fault_type == 'sa1':
                    ctrl_difficulty = CC0   # need to force 0 -- CC0 is the cost
                else:
                    ctrl_difficulty = CC1   # need to force 1 -- CC1 is the cost

                if ctrl_difficulty < CP_MIN_CTRL:
                    continue
                if CO < CP_MIN_CO:
                    continue

                CC_diff = abs(CC0 - CC1)
                # Balanced score: controllability difficulty + observability + imbalance
                cp_score = (ctrl_difficulty * 0.4) + (CO * 0.4) + (CC_diff * 0.2)

                cp_candidates.append({
                    'node'            : node,
                    'type'            : 'CP',
                    'fault_type'      : fault_type,
                    'class'           : fault_class,
                    'cell'            : cell,
                    'hier'            : hier,
                    'CC0'             : CC0,
                    'CC1'             : CC1,
                    'CO'              : CO,
                    'CC_diff'         : CC_diff,
                    'ctrl_difficulty' : ctrl_difficulty,
                    'score'           : round(cp_score, 2),
                })

            # ------------------------------------------------------------------
            # OP selection: NO faults on output pins (/Y, /Q, /QN)
            # We want nodes with high CO (hard to observe) and moderate CC
            # (reachable, so the observation point actually fires)
            # ------------------------------------------------------------------
            elif fault_class == 'NO' and pin in ('Y', 'Q', 'QN', 'CO', 'S'):
                min_ctrl = min(CC0, CC1)

                if CO < OP_MIN_CO:
                    continue
                if min_ctrl > OP_MAX_CTRL:
                    continue

                # OP score: prioritise high CO and easy-to-reach nodes
                op_score = (CO * 0.6) + (min_ctrl * 0.4)

                op_candidates.append({
                    'node'       : node,
                    'type'       : 'OP',
                    'fault_type' : fault_type,
                    'class'      : fault_class,
                    'cell'       : cell,
                    'hier'       : hier,
                    'CC0'        : CC0,
                    'CC1'        : CC1,
                    'CO'         : CO,
                    'min_ctrl'   : min_ctrl,
                    'score'      : round(op_score, 2),
                })

    return cp_candidates, op_candidates


# ---------------------------------------------------------------------------
# Enforce hierarchy diversity: at most MAX_PER_HIER candidates per block
# ---------------------------------------------------------------------------
def diversify(candidates, max_per_hier=MAX_PER_HIER):
    seen  = {}
    result = []
    for c in candidates:
        h = c['hier']
        count = seen.get(h, 0)
        if count < max_per_hier:
            seen[h] = count + 1
            result.append(c)
    return result


# ---------------------------------------------------------------------------
# Print a ranked table to stdout
# ---------------------------------------------------------------------------
def print_table(ranked, title, score_key='score'):
    print('\n' + '=' * 95)
    print('  %s' % title)
    print('=' * 95)
    print('%-5s %-25s %-4s %-5s %-6s %-6s %-6s %-8s %-10s %s' % (
        'Rank', 'Node', 'Type', 'FT', 'CC0', 'CC1', 'CO', 'Hier', 'Score', 'Cell'
    ))
    print('-' * 95)
    for i, c in enumerate(ranked[:20], 1):
        print('%-5d %-25s %-4s %-5s %-6d %-6d %-6d %-8s %-10.2f %s' % (
            i,
            c['node'][:25],
            c['type'],
            c['fault_type'],
            c['CC0'],
            c['CC1'],
            c['CO'],
            c['hier'][:8],
            c[score_key],
            c['cell'],
        ))


# ---------------------------------------------------------------------------
# Export top-N candidates to CSV
# ---------------------------------------------------------------------------
def export_csv(ranked, fname, n):
    top_n = ranked[:n]
    if not top_n:
        print('  Warning: no candidates to export for %s' % fname)
        return []

    with open(fname, 'w') as f:
        writer = csv.DictWriter(f, fieldnames=list(top_n[0].keys()))
        writer.writeheader()
        writer.writerows(top_n)

    print('  Exported top %d --> %s' % (n, fname))
    return top_n


# ---------------------------------------------------------------------------
# Process one circuit
# ---------------------------------------------------------------------------
def process_circuit(circuit, fault_file, itp_counts):
    print('\n' + '#' * 95)
    print('  CIRCUIT: %s   |   Fault file: %s' % (circuit.upper(), fault_file))
    print('#' * 95)

    if not os.path.exists(fault_file):
        print('  ERROR: File not found -- %s  (skipping)' % fault_file)
        return None

    cp_raw, op_raw = parse_verbose_faults(fault_file)

    if not cp_raw and not op_raw:
        print('  ERROR: No candidates found in %s' % fault_file)
        return None

    print('\n  Raw CP candidates (NC /Y faults) : %d' % len(cp_raw))
    print('  Raw OP candidates (NO /Y,Q,QN)   : %d' % len(op_raw))

    # Sort before diversifying
    cp_sorted = sorted(cp_raw, key=lambda x: x['score'], reverse=True)
    op_sorted = sorted(op_raw, key=lambda x: x['score'], reverse=True)

    # Apply hierarchy diversity filter
    cp_ranked = diversify(cp_sorted)
    op_ranked = diversify(op_sorted)

    print('  After hierarchy deduplication:')
    print('    CP candidates : %d' % len(cp_ranked))
    print('    OP candidates : %d' % len(op_ranked))

    # Print top-20 tables
    print_table(cp_ranked, 'Control Points (CP) -- %s' % circuit)
    print_table(op_ranked, 'Observation Points (OP) -- %s' % circuit)

    # Build combined list: interleave CP and OP for balanced mixed insertion
    # Normalise fields so CP and OP rows share the same CSV columns
    COMBINED_FIELDS = ['node', 'type', 'fault_type', 'class', 'cell',
                       'hier', 'CC0', 'CC1', 'CO', 'score']

    def normalise(c):
        return {k: c.get(k, '') for k in COMBINED_FIELDS}

    combined = []
    ci, oi = 0, 0
    while ci < len(cp_ranked) or oi < len(op_ranked):
        if ci < len(cp_ranked):
            combined.append(normalise(cp_ranked[ci])); ci += 1
        if oi < len(op_ranked):
            combined.append(normalise(op_ranked[oi])); oi += 1

    results = {}

    for n in itp_counts:
        print('\n  --- Top %d ---' % n)

        cp_fname  = '%s_cp_top%d.csv'       % (circuit, n)
        op_fname  = '%s_op_top%d.csv'       % (circuit, n)
        mix_fname = '%s_combined_top%d.csv' % (circuit, n)

        export_csv(cp_ranked, cp_fname,  n)
        export_csv(op_ranked, op_fname,  n)
        export_csv(combined,  mix_fname, n)

        results[n] = {
            'cp'       : cp_ranked[:n],
            'op'       : op_ranked[:n],
            'combined' : combined[:n],
        }

    return results


# ---------------------------------------------------------------------------
# Write master summary CSV
# ---------------------------------------------------------------------------
def write_summary(all_results, itp_counts):
    fname = 'itp_selection_summary.csv'
    rows  = []

    for circuit, by_count in all_results.items():
        for n, data in by_count.items():
            for tp_type, candidates in data.items():
                nodes = ' | '.join(c['node'] for c in candidates)
                rows.append({
                    'circuit'   : circuit,
                    'tp_type'   : tp_type,
                    'itp_count' : n,
                    'nodes'     : nodes,
                })

    with open(fname, 'w') as f:
        writer = csv.DictWriter(
            f, fieldnames=['circuit', 'tp_type', 'itp_count', 'nodes'])
        writer.writeheader()
        writer.writerows(rows)

    print('\n  Master summary --> %s' % fname)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    circuits = {
        'b14' : 'b14_ND_verbose.rpt',
        'b15' : 'b15_ND_verbose.rpt',
        'b17' : 'b17_ND_verbose.rpt',
    }

    itp_counts = [5, 10, 15]

    if len(sys.argv) > 1:
        selected = sys.argv[1:]
        circuits = {k: v for k, v in circuits.items() if k in selected}
        if not circuits:
            print('Error: unknown circuit(s) %s' % str(sys.argv[1:]))
            print('Valid options: b14  b15  b17')
            sys.exit(1)

    all_results = {}

    for circuit, fault_file in circuits.items():
        result = process_circuit(circuit, fault_file, itp_counts)
        if result:
            all_results[circuit] = result

    if all_results:
        write_summary(all_results, itp_counts)

    print('\n' + '=' * 95)
    print('  DONE -- processed %d circuit(s)' % len(all_results))
    print('=' * 95 + '\n')


if __name__ == '__main__':
    main()
