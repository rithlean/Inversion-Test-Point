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
            m = re.match(
                r'(sa[01])\s+(NC|NO)\s+(\S+)\s+'
                r'\((\S+)\)\s+'
                r'\(\d+:\s*\S+,\s*'
                r'SCOAP\s*=\s*(\d+)/(\d+)/(\d+)'
                r'\s+(\d+)/(\d+)/(\d+)',
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

            # Skip reset pins and duplicates
            if any(x in node for x in ['RSTB','RST','reset']):
                continue
            if node in seen:
                continue
            seen.add(node)

            # Skip non-NC faults for XOR ITP
            if fault_class != 'NC':
                continue

            CC_diff = abs(CC0 - CC1)
            CC_max  = max(CC0, CC1)

            # Method 1 — Raw CC0/CC1
            # sa1 NC → rank by CC0 (hard to set to 0)
            # sa0 NC → rank by CC1 (hard to set to 1)
            if fault_type == 'sa1':
                raw_score = CC0
            else:
                raw_score = CC1

            # Method 2 — Weighted scoring
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

    print(f"\n{'='*90}")
    print(f"  {method_name} — {circuit}")
    print(f"{'='*90}")
    print(f"{'Rank':<5} {'Node':<20} {'FT':<5} {'CC0':<6} {'CC1':<6} "
          f"{'CO':<6} {'CCdiff':<8} {'Score':<10} {'Cell'}")
    print("-" * 90)

    for i, c in enumerate(ranked[:20], 1):
        print(f"{i:<5} {c['node']:<20} {c['fault_type']:<5} "
              f"{c['CC0']:<6} {c['CC1']:<6} {c['CO']:<6} "
              f"{c['CC_diff']:<8} {c[score_key]:<10.1f} {c['cell']}")

    # Export CSVs for each ITP count
    for n in itp_counts:
        if n > len(ranked):
            print(f"\n  Warning: only {len(ranked)} candidates available "
                  f"— cannot export top {n}")
            continue

        top_n = ranked[:n]
        fname = f'{circuit}_{method_name.lower().replace(" ","_")}_top{n}.csv'

        with open(fname, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=top_n[0].keys())
            writer.writeheader()
            writer.writerows(top_n)

        print(f"\n  Top {n} nodes → {fname}")
        for c in top_n:
            print(f"    {c['node']:<20} CC0={c['CC0']:<5} CC1={c['CC1']:<5} "
                  f"CO={c['CO']:<5} score={c[score_key]:.1f}")

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

        print(f"\n{'='*60}")
        print(f"  Method comparison — {circuit} top {n} nodes")
        print(f"{'='*60}")
        print(f"  Nodes in BOTH methods : {len(common)}")
        print(f"  Raw only              : {len(raw_only)}")
        print(f"  Weighted only         : {len(wtd_only)}")

        print(f"\n  Common nodes (high confidence ITP targets):")
        for node in sorted(common):
            print(f"    {node}")

        print(f"\n  Raw only:")
        for node in sorted(raw_only):
            print(f"    {node}")

        print(f"\n  Weighted only:")
        for node in sorted(wtd_only):
            print(f"    {node}")


def write_summary(all_results, itp_counts):
    fname = 'itp_selection_summary.csv'
    rows  = []

    for circuit, methods in all_results.items():
        for method_name, ranked in methods.items():
            for n in itp_counts:
                top_n = ranked[:n]
                nodes = [c['node'] for c in top_n]
                rows.append({
                    'circuit'     : circuit,
                    'method'      : method_name,
                    'itp_count'   : n,
                    'nodes'       : ' | '.join(nodes),
                })

    with open(fname, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['circuit','method',
                                               'itp_count','nodes'])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n  Master summary saved → {fname}")


def process_circuit(circuit, fault_file, itp_counts):
    print(f"\n{'#'*90}")
    print(f"  PROCESSING CIRCUIT: {circuit.upper()}")
    print(f"  Fault file: {fault_file}")
    print(f"{'#'*90}")

    # Check file exists
    if not os.path.exists(fault_file):
        print(f"\n  ERROR: File not found — {fault_file}")
        print(f"  Skipping {circuit}")
        return None

    candidates = parse_verbose_faults(fault_file)

    if not candidates:
        print(f"\n  ERROR: No NC fault candidates found in {fault_file}")
        print(f"  Check that report_faults -class ND -verbose was run correctly")
        return None

    print(f"\n  Total NC gate-level candidates : {len(candidates)}")

    # Count sa0 vs sa1
    sa0_count = sum(1 for c in candidates if c['fault_type'] == 'sa0')
    sa1_count = sum(1 for c in candidates if c['fault_type'] == 'sa1')
    print(f"  sa1 NC faults (high CC0)       : {sa1_count}")
    print(f"  sa0 NC faults (high CC1)       : {sa0_count}")

    results = {}

    # Method 1 — Raw
    ranked_raw = export_and_print(
        candidates, 'Method1_Raw', 'raw_score', circuit, itp_counts
    )
    results['Method1_Raw'] = ranked_raw

    # Method 2 — Weighted
    ranked_weighted = export_and_print(
        candidates, 'Method2_Weighted', 'weighted_score', circuit, itp_counts
    )
    results['Method2_Weighted'] = ranked_weighted

    # Comparison
    compare_methods(candidates, circuit, itp_counts)

    return results


def main():
    # ── Configuration ────────────────────────────────────────────
    # Map each circuit to its verbose fault report file
    circuits = {
        'b14' : 'b14_ND_verbose.rpt',
        'b15' : 'b15_ND_verbose.rpt',
        'b17' : 'b17_ND_verbose.rpt',
    }

    # ITP counts to evaluate
    itp_counts = [5, 10, 15]

    # ── Optional: run specific circuit from command line ─────────
    # Usage: python3 itp_selection.py b14
    #        python3 itp_selection.py b14 b15
    #        python3 itp_selection.py          (runs all)
    if len(sys.argv) > 1:
        selected = sys.argv[1:]
        circuits = {k: v for k, v in circuits.items() if k in selected}
        if not circuits:
            print(f"Error: unknown circuit(s) {sys.argv[1:]}")
            print(f"Valid options: b14 b15 b17")
            sys.exit(1)

    # ── Run ──────────────────────────────────────────────────────
    all_results = {}

    for circuit, fault_file in circuits.items():
        result = process_circuit(circuit, fault_file, itp_counts)
        if result:
            all_results[circuit] = result

    # ── Master summary CSV ────────────────────────────────────────
    if all_results:
        write_summary(all_results, itp_counts)

    print(f"\n{'='*90}")
    print(f"  DONE — processed {len(all_results)} circuit(s)")
    print(f"{'='*90}\n")


if __name__ == '__main__':
    main()