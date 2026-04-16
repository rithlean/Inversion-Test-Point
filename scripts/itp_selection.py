# -*- coding: utf-8 -*-
# =============================================================================
# ITP Selection Script  --  Python 2 Compatible Version
# =============================================================================

import re
import csv
import sys
import os

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
CP_MIN_CTRL   = 5
CP_MIN_CO     = 3
OP_MIN_CO     = 20
OP_MAX_CTRL   = 200
MAX_PER_HIER  = 1


# ---------------------------------------------------------------------------
# Discover circuits
# ---------------------------------------------------------------------------
def discover_fault_files():
    circuits = {}
    files = os.listdir('.')

    for fname in files:
        if fname.endswith('.rpt') and 'verbose' in fname:
            circuit = fname.replace('_ND_verbose.rpt', '')
            circuit = circuit.replace('_verbose.rpt', '')
            circuits[circuit] = fname

    return circuits


# ---------------------------------------------------------------------------
# Parse fault report
# ---------------------------------------------------------------------------
def parse_verbose_faults(filepath):

    cp_candidates = []
    op_candidates = []
    seen_nodes = set()

    f = open(filepath, 'r')

    for line in f:
        line = line.strip()

        if 'SCOAP=' not in line:
            continue

        parts = line.split()
        if len(parts) < 3:
            continue

        if parts[0] not in ['sa0', 'sa1']:
            continue

        fault_type  = parts[0]
        fault_class = parts[1]
        node        = parts[2]

        if fault_class not in ['NC', 'NO']:
            continue

        # Cell extraction (safe)
        cell_m = re.search(r'\(([A-Za-z0-9_]+)\)', line)
        if cell_m:
            cell = cell_m.group(1)
        else:
            cell = 'NA'

        # SCOAP extraction
        scoap_m = re.search(r'SCOAP=(\d+)/(\d+)/(\d+)', line)
        if not scoap_m:
            continue

        CC0 = int(scoap_m.group(1))
        CC1 = int(scoap_m.group(2))
        CO  = int(scoap_m.group(3))

        # Skip reset/set
        if ('rst' in node.lower()) or ('set' in node.lower()):
            continue

        key = (node, fault_type)
        if key in seen_nodes:
            continue
        seen_nodes.add(key)

        node_parts = node.split('/')

        if len(node_parts) >= 2:
            hier = node_parts[0]
            pin  = node_parts[-1]
        else:
            hier = 'top'
            pin  = node

        # ------------------------------------------------------------------
        # CP selection
        # ------------------------------------------------------------------
        if fault_class == 'NC' and pin == 'Y':

            if fault_type == 'sa1':
                ctrl_difficulty = CC0
            else:
                ctrl_difficulty = CC1

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
        elif fault_class == 'NO' and pin in ['Y', 'Q', 'QN']:

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

    f.close()

    return cp_candidates, op_candidates


# ---------------------------------------------------------------------------
# Diversity filter
# ---------------------------------------------------------------------------
def diversify(candidates):

    seen = {}
    result = []

    for c in candidates:
        h = c['hier']

        if h not in seen:
            seen[h] = 0

        if seen[h] < MAX_PER_HIER:
            seen[h] += 1
            result.append(c)

    return result


# ---------------------------------------------------------------------------
# Print table
# ---------------------------------------------------------------------------
def print_table(ranked, title):

    print('')
    print('=' * 90)
    print('  ' + title)
    print('=' * 90)

    print('%-5s %-25s %-4s %-5s %-6s %-6s %-6s %-10s %-10s %s' %
          ('Rank', 'Node', 'Type', 'FT', 'CC0', 'CC1', 'CO', 'Hier', 'Score', 'Cell'))

    print('-' * 90)

    i = 1
    for c in ranked[:20]:

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
            c['cell']
        ))

        i += 1


# ---------------------------------------------------------------------------
# Export CSV
# ---------------------------------------------------------------------------
def export_csv(ranked, fname, n):

    top_n = ranked[:n]

    if len(top_n) == 0:
        print('  Warning: empty export ' + fname)
        return []

    f = open(fname, 'wb')
    writer = csv.DictWriter(f, fieldnames=top_n[0].keys())

    writer.writeheader()
    writer.writerows(top_n)

    f.close()

    print('  Exported top %d --> %s' % (n, fname))

    return top_n


# ---------------------------------------------------------------------------
# Process circuit
# ---------------------------------------------------------------------------
def process_circuit(circuit, fault_file, itp_counts):

    print('')
    print('#' * 90)
    print('  CIRCUIT: %s  FILE: %s' % (circuit.upper(), fault_file))
    print('#' * 90)

    cp_raw, op_raw = parse_verbose_faults(fault_file)

    print('  Raw CP:', len(cp_raw))
    print('  Raw OP:', len(op_raw))

    cp_sorted = sorted(cp_raw, key=lambda x: x['score'], reverse=True)
    op_sorted = sorted(op_raw, key=lambda x: x['score'], reverse=True)

    cp_ranked = diversify(cp_sorted)
    op_ranked = diversify(op_sorted)

    print('  After diversity CP=%d OP=%d' % (len(cp_ranked), len(op_ranked)))

    print_table(cp_ranked, 'CP - ' + circuit)
    print_table(op_ranked, 'OP - ' + circuit)

    combined = []
    ci = 0
    oi = 0

    while ci < len(cp_ranked) or oi < len(op_ranked):

        if ci < len(cp_ranked):
            combined.append(cp_ranked[ci])
            ci += 1

        if oi < len(op_ranked):
            combined.append(op_ranked[oi])
            oi += 1

    results = {}

    for n in itp_counts:

        print('  --- Top %d ---' % n)

        export_csv(cp_ranked, circuit + '_cp_top' + str(n) + '.csv', n)
        export_csv(op_ranked, circuit + '_op_top' + str(n) + '.csv', n)
        export_csv(combined, circuit + '_combined_top' + str(n) + '.csv', n)

        results[n] = {
            'cp': cp_ranked[:n],
            'op': op_ranked[:n],
            'combined': combined[:n]
        }

    return results


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def write_summary(all_results):

    fname = 'itp_selection_summary.csv'
    f = open(fname, 'wb')

    writer = csv.DictWriter(f, fieldnames=['circuit', 'tp_type', 'itp_count', 'nodes'])
    writer.writeheader()

    for circuit in all_results:

        for n in all_results[circuit]:

            data = all_results[circuit][n]

            for tp_type in data:

                nodes = ' | '.join([c['node'] for c in data[tp_type]])

                writer.writerow({
                    'circuit': circuit,
                    'tp_type': tp_type,
                    'itp_count': n,
                    'nodes': nodes
                })

    f.close()

    print('  Summary written: ' + fname)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():

    itp_counts = [5, 10, 15]

    circuits = discover_fault_files()

    if len(sys.argv) > 1:
        selected = sys.argv[1:]
        circuits = dict((k, v) for k, v in circuits.items() if k in selected)

    if len(circuits) == 0:
        print('ERROR: no circuits found')
        return

    all_results = {}

    for circuit in circuits:
        result = process_circuit(circuit, circuits[circuit], itp_counts)
        all_results[circuit] = result

    write_summary(all_results)

    print('')
    print('DONE')


if __name__ == '__main__':
    main()
