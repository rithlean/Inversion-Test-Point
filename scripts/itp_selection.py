# -*- coding: utf-8 -*-
import re
import csv
import sys
import os

def parse_verbose_faults(filepath):
    candidates = []
    seen = set()

    with open(filepath) as f:
        for line in f:
            line = line.strip()

            # Match exact TetraMAX format:
            # sa1   NC   U1503/Y   (INVX8_LVT)   ( 1: 52/1/0, SCOAP=1/1/1 0/0/0/0 )
            m = re.match(
                r'(sa[01])\s+(NC|NO)\s+(\S+)\s+'
                r'\((\S+)\)\s+'
                r'\(\s*\d+:\s*\S+,\s*'
                r'SCOAP=(\d+)/(\d+)/(\d+)\s+'
                r'(\d+)/(\d+)/(\d+)\s*\)',
                line
            )
            if not m:
                continue

            fault_type  = m.group(1)
            fault_class = m.group(2)
            node        = m.group(3)
            cell        = m.group(4)
            CC0         = int(m.group(5))
            CC1         = int(m.group(6))
            CO          = int(m.group(7))

            if any(x in node for x in ['RSTB', 'RST', 'reset']):
                continue
            if node in seen:
                continue
            seen.add(node)

            if fault_class != 'NC':
                continue

            CC_diff = abs(CC0 - CC1)
            CC_max  = max(CC0, CC1)

            if fault_type == 'sa1':
                raw_score = CC0
            else:
                raw_score = CC1

            weighted_score = CC_diff * 0.5 + CC_max * 0.3 + CO * 0.2

            candidates.append({
                'node'           : node,
                'fault_type'     : fault_type,
                'class'          : fault_class,
                'cell'           : cell,
                'CC0'            : CC0,
                'CC1'            : CC1,
                'CO'             : CO,
                'CC_diff'        : CC_diff,
                'CC_max'         : CC_max,
                'raw_score'      : raw_score,
                'weighted_score' : weighted_score,
            })

    return candidates


def export_and_print(candidates, method_name, score_key, circuit, itp_counts):
    ranked = sorted(candidates, key=lambda x: x[score_key], reverse=True)

    print("\n" + "="*90)
    print("  %s -- %s" % (method_name, circuit))
    print("="*90)
    print("%-5s %-20s %-5s %-6s %-6s %-6s %-8s %-10s %s" % (
        'Rank', 'Node', 'FT', 'CC0', 'CC1', 'CO', 'CCdiff', 'Score', 'Cell'
    ))
    print("-" * 90)

    for i, c in enumerate(ranked[:20], 1):
        print("%-5d %-20s %-5s %-6d %-6d %-6d %-8d %-10.1f %s" % (
            i,
            c['node'],
            c['fault_type'],
            c['CC0'],
            c['CC1'],
            c['CO'],
            c['CC_diff'],
            c[score_key],
            c['cell']
        ))

    for n in itp_counts:
        if n > len(ranked):
            print("\n  Warning: only %d candidates -- cannot export top %d"
                  % (len(ranked), n))
            continue

        top_n = ranked[:n]
        fname = '%s_%s_top%d.csv' % (
            circuit,
            method_name.lower().replace(' ', '_'),
            n
        )

        with open(fname, 'w') as f:
            writer = csv.DictWriter(f, fieldnames=list(top_n[0].keys()))
            writer.writeheader()
            writer.writerows(top_n)

        print("\n  Top %d nodes --> %s" % (n, fname))
        for c in top_n:
            print("    %-20s CC0=%-5d CC1=%-5d CO=%-5d score=%.1f" % (
                c['node'], c['CC0'], c['CC1'], c['CO'], c[score_key]
            ))

    return ranked


def compare_methods(candidates, circuit, itp_counts):
    for n in itp_counts:
        ranked_raw      = sorted(candidates,
                                 key=lambda x: x['raw_score'],
                                 reverse=True)
        ranked_weighted = sorted(candidates,
                                 key=lambda x: x['weighted_score'],
                                 reverse=True)

        raw_top      = set(c['node'] for c in ranked_raw[:n])
        weighted_top = set(c['node'] for c in ranked_weighted[:n])

        common   = raw_top & weighted_top
        raw_only = raw_top - weighted_top
        wtd_only = weighted_top - raw_top

        print("\n" + "="*60)
        print("  Method comparison -- %s top %d nodes" % (circuit, n))
        print("="*60)
        print("  Nodes in BOTH methods : %d" % len(common))
        print("  Raw only              : %d" % len(raw_only))
        print("  Weighted only         : %d" % len(wtd_only))

        print("\n  Common nodes (high confidence ITP targets):")
        for node in sorted(common):
            print("    %s" % node)

        print("\n  Raw only:")
        for node in sorted(raw_only):
            print("    %s" % node)

        print("\n  Weighted only:")
        for node in sorted(wtd_only):
            print("    %s" % node)


def write_summary(all_results, itp_counts):
    fname = 'itp_selection_summary.csv'
    rows  = []

    for circuit, methods in all_results.items():
        for method_name, ranked in methods.items():
            for n in itp_counts:
                top_n = ranked[:n]
                nodes = [c['node'] for c in top_n]
                rows.append({
                    'circuit'   : circuit,
                    'method'    : method_name,
                    'itp_count' : n,
                    'nodes'     : ' | '.join(nodes),
                })

    with open(fname, 'w') as f:
        writer = csv.DictWriter(f,
                                fieldnames=['circuit', 'method',
                                            'itp_count', 'nodes'])
        writer.writeheader()
        writer.writerows(rows)

    print("\n  Master summary saved --> %s" % fname)


def process_circuit(circuit, fault_file, itp_counts):
    print("\n" + "#"*90)
    print("  PROCESSING CIRCUIT: %s" % circuit.upper())
    print("  Fault file: %s" % fault_file)
    print("#"*90)

    if not os.path.exists(fault_file):
        print("\n  ERROR: File not found -- %s" % fault_file)
        print("  Skipping %s" % circuit)
        return None

    candidates = parse_verbose_faults(fault_file)

    if not candidates:
        print("\n  ERROR: No NC fault candidates found in %s" % fault_file)
        print("  Check that report_faults -class ND -verbose was run correctly")
        return None

    sa0_count = sum(1 for c in candidates if c['fault_type'] == 'sa0')
    sa1_count = sum(1 for c in candidates if c['fault_type'] == 'sa1')

    print("\n  Total NC gate-level candidates : %d" % len(candidates))
    print("  sa1 NC faults (high CC0)       : %d" % sa1_count)
    print("  sa0 NC faults (high CC1)       : %d" % sa0_count)

    results = {}

    ranked_raw = export_and_print(
        candidates, 'Method1_Raw', 'raw_score', circuit, itp_counts
    )
    results['Method1_Raw'] = ranked_raw

    ranked_weighted = export_and_print(
        candidates, 'Method2_Weighted', 'weighted_score', circuit, itp_counts
    )
    results['Method2_Weighted'] = ranked_weighted

    compare_methods(candidates, circuit, itp_counts)

    return results


def main():
    circuits = {
        'b14' : 'b14_ND_verbose.rpt',
        'b15' : 'b15_ND_verbose.rpt',
        'b17' : 'b17_ND_verbose.rpt',
    }

    itp_counts = [5, 10, 15]

    if len(sys.argv) > 1:
        selected = sys.argv[1:]
        circuits = dict((k, v) for k, v in circuits.items() if k in selected)
        if not circuits:
            print("Error: unknown circuit(s) %s" % str(sys.argv[1:]))
            print("Valid options: b14 b15 b17")
            sys.exit(1)

    all_results = {}

    for circuit, fault_file in circuits.items():
        result = process_circuit(circuit, fault_file, itp_counts)
        if result:
            all_results[circuit] = result

    if all_results:
        write_summary(all_results, itp_counts)

    print("\n" + "="*90)
    print("  DONE -- processed %d circuit(s)" % len(all_results))
    print("="*90 + "\n")


if __name__ == '__main__':
    main()
