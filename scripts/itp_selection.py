# -*- coding: utf-8 -*-
# =============================================================================
# ITP Selection Script  --  Generalised (ITC'99 + ISCAS'89 + Any Circuit)
# =============================================================================

import re
import csv
import sys
import os

# ---------------------------------------------------------------------------
# Thresholds (tune for experiments)
# ---------------------------------------------------------------------------
CP_MIN_CTRL   = 5
CP_MIN_CO     = 3
OP_MIN_CO     = 20
OP_MAX_CTRL   = 200
MAX_PER_HIER  = 1

OUTPUT_PINS = ('Y', 'Z', 'ZN', 'Q', 'QN', 'OUT', 'O', 'S', 'CO')


# ---------------------------------------------------------------------------
# Auto-discover fault reports
# ---------------------------------------------------------------------------
def discover_fault_files():
    circuits = {}
    for fname in os.listdir('.'):
        if '_verbose.rpt' in fname:
            circuit = fname.replace('_verbose.rpt', '').replace('_ND', '')
            circuits[circuit] = fname
    return circuits


# ---------------------------------------------------------------------------
# Parse TetraMAX verbose report
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

            fault_type  = parts[0]
            fault_class = parts[1]
            node        = parts[2]

            if fault_class not in ('NC', 'NO'):
                continue

            # Extract cell (optional)
            cell_m = re.search(r'\(([A-Za-z0-9_]+)\)', line)
            cell = cell_m.group(1) if cell_m else 'NA'

            # Extract SCOAP
            scoap_m = re.search(r'SCOAP=(\d+)/(\d+)/(\d+)', line)
            if not scoap_m:
                continue

            CC0 = int(scoap_m.group(1))
            CC1 = int(scoap_m.group(2))
            CO  = int(scoap_m.group(3))

            # Skip reset/set
            if any(x.lower() in node.lower() for x in ['rst', 'set']):
                continue

            key = (node, fault_type)
            if key in seen_nodes:
                continue
            seen_nodes.add(key)

            node_parts = node.split('/')

            # Improved hierarchy handling (fix ISCAS flat issue)
            if len(node_parts) >= 3:
                hier = node_parts[0]
            elif len(node_parts) == 2:
                hier = node_parts[0]
            else:
                hier = node  # ← FIX (not 'top')

            pin = node_parts[-1] if len(node_parts) > 1 else node

            # ------------------------------------------------------------------
            # CP selection
            # ------------------------------------------------------------------
            if fault_class == 'NC' and pin in OUTPUT_PINS:

                ctrl_difficulty = CC0 if fault_type == 'sa1' else CC1

                if ctrl_difficulty < CP_MIN_CTRL:
                    continue
                if CO < CP_MIN_CO:
                    continue

                CC_diff = abs(CC0 - CC1)

                cp_score = (
                    0.4 * ctrl_difficulty +
                    0.4 * CO +
                    0.2 * CC_diff
                )

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
            # OP selection
            # ------------------------------------------------------------------
            elif fault_class == 'NO' and pin in OUTPUT_PINS:

                min_ctrl = min(CC0, CC1)

                if CO < OP_MIN_CO:
                    continue
                if min_ctrl > OP_MAX_CTRL:
                    continue

                op_score = (
                    0.6 * CO +
                    0.4 * min_ctrl
                )

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
# Diversity filter
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
# Print table
# ---------------------------------------------------------------------------
def print_table(ranked, title):
    print('\n' + '=' * 95)
    print('  %s' % title)
    print('=' * 95)
    print('%-5s %-25s %-4s %-5s %-6s %-6s %-6s %-10s %-10s %s' % (
        'Rank', 'Node', 'Type', 'FT', 'CC0', 'CC1', 'CO', 'Hier', 'Score', 'Cell'
    ))
    print('-' * 95)

    for i, c in enumerate(ranked[:20], 1):
        print('%-5d %-25s %-4s %-5s %-6d %-6d %-6d %-10s %-10.2f %s' % (
            i,
            c['node'][:25],
            c['type'],
            c['fault_type'],
            c['CC0'],
            c['CC1'],
            c['CO'],
            c['hier'][:10],
            c['score'],
            c['cell'],
        ))


# ---------------------------------------------------------------------------
# Export CSV
# ---------------------------------------------------------------------------
def export_csv(ranked, fname, n):
    top_n = ranked[:n]
    if not top_n:
        return []

    with open(fname, 'w') as f:
        writer = csv.DictWriter(f, fieldnames=list(top_n[0].keys()))
        writer.writeheader()
        writer.writerows(top_n)

    print(f'  Exported top {n} --> {fname}')
    return top_n


# ---------------------------------------------------------------------------
# Process one circuit
# ---------------------------------------------------------------------------
def process_circuit(circuit, fault_file, itp_counts):
    print('\n' + '#' * 95)
    print(f'  CIRCUIT: {circuit.upper()}   |   File: {fault_file}')
    print('#' * 95)

    cp_raw, op_raw = parse_verbose_faults(fault_file)

    print(f'\n  Raw CP: {len(cp_raw)}')
    print(f'  Raw OP: {len(op_raw)}')

    cp_sorted = sorted(cp_raw, key=lambda x: x['score'], reverse=True)
    op_sorted = sorted(op_raw, key=lambda x: x['score'], reverse=True)

    cp_ranked = diversify(cp_sorted)
    op_ranked = diversify(op_sorted)

    print(f'  After diversity: CP={len(cp_ranked)}, OP={len(op_ranked)}')

    print_table(cp_ranked, f'CP -- {circuit}')
    print_table(op_ranked, f'OP -- {circuit}')

    combined = []
    ci, oi = 0, 0
    while ci < len(cp_ranked) or oi < len(op_ranked):
        if ci < len(cp_ranked):
            combined.append(cp_ranked[ci]); ci += 1
        if oi < len(op_ranked):
            combined.append(op_ranked[oi]); oi += 1

    results = {}

    for n in itp_counts:
        print(f'\n  --- Top {n} ---')

        export_csv(cp_ranked, f'{circuit}_cp_top{n}.csv', n)
        export_csv(op_ranked, f'{circuit}_op_top{n}.csv', n)
        export_csv(combined,  f'{circuit}_combined_top{n}.csv', n)

        results[n] = {
            'cp': cp_ranked[:n],
            'op': op_ranked[:n],
            'combined': combined[:n],
        }

    return results


# ---------------------------------------------------------------------------
# Summary CSV
# ---------------------------------------------------------------------------
def write_summary(all_results, itp_counts):
    with open('itp_selection_summary.csv', 'w') as f:
        writer = csv.DictWriter(
            f, fieldnames=['circuit', 'tp_type', 'itp_count', 'nodes'])
        writer.writeheader()

        for circuit, by_count in all_results.items():
            for n, data in by_count.items():
                for tp_type, candidates in data.items():
                    nodes = ' | '.join(c['node'] for c in candidates)
                    writer.writerow({
                        'circuit': circuit,
                        'tp_type': tp_type,
                        'itp_count': n,
                        'nodes': nodes,
                    })

    print('\n  Master summary generated')


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    itp_counts = [5, 10, 15]

    circuits = discover_fault_files()

    if len(sys.argv) > 1:
        selected = sys.argv[1:]
        circuits = {k: v for k, v in circuits.items() if k in selected}

    if not circuits:
        print('ERROR: No matching circuits found')
        sys.exit(1)

    all_results = {}

    for circuit, fault_file in circuits.items():
        result = process_circuit(circuit, fault_file, itp_counts)
        if result:
            all_results[circuit] = result

    if all_results:
        write_summary(all_results, itp_counts)

    print('\nDONE.')


if __name__ == '__main__':
    main()
