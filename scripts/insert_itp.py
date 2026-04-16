# -*- coding: utf-8 -*-
# =============================================================================
# Universal ITP Insertion Script  --  Control Points (CP) + Observation Points (OP)
# =============================================================================
# Usage:
#   python insert_itp.py <netlist.v> <candidates.csv> <n_itps> [--mode cp|op|mixed]
#
# Examples:
#   python insert_itp.py b15_scan.v b15_cp_top15.csv       10 --mode cp
#   python insert_itp.py b15_scan.v b15_op_top15.csv       10 --mode op
#   python insert_itp.py b15_scan.v b15_combined_top15.csv 15 --mode mixed
#   python insert_itp.py b14_scan.v b14_cp_top15.csv        5 --mode cp
#   python insert_itp.py b17_scan.v b17_combined_top15.csv 15 --mode mixed
#
# What it does:
#   CP mode  : Inserts XOR2 gate on the driven net of each /Y candidate.
#              Original net --> XOR(net, 1'b1) --> itp_net (always inverting in test).
#              All downstream consumers of the original net are redirected to itp_net.
#
#   OP mode  : Inserts a scan observation tap for each /Y, /Q, /QN candidate.
#              Taps the net into an SDFF scan cell (SDFFARX1_LVT) configured as
#              an observation flip-flop. The observation FF is connected to the
#              existing scan chain via dedicated scan_obs_in/scan_obs_out ports.
#
#   mixed    : Reads the 'type' column from the combined CSV and applies CP or OP
#              insertion automatically per candidate.
#
# Configuration:
#   Edit the LIBRARY CONFIG section below if your cell library uses different names.
#
# Outputs:
#   <circuit>_itp<N>.v           Modified netlist with ITPs inserted
#   <circuit>_itp<N>_report.txt  Insertion report with SCOAP values and stats
# =============================================================================

import re
import csv
import sys
import os

# =============================================================================
# LIBRARY CONFIG -- change these to match your cell library
# =============================================================================
XOR_CELL        = 'XOR2X1_LVT'     # XOR gate for CP insertion
XOR_PORT_A      = 'A1'             # Data input port of XOR
XOR_PORT_B      = 'A2'             # Control input port of XOR (tied to 1'b1)
XOR_PORT_Y      = 'Y'              # Output port of XOR

OBS_FF_CELL     = 'SDFFARX1_LVT'  # Scan DFF for OP insertion
OBS_FF_D        = 'D'              # Data input
OBS_FF_SI       = 'SI'             # Scan input
OBS_FF_SE       = 'SE'             # Scan enable
OBS_FF_RSTB     = 'RSTB'          # Active-low async reset
OBS_FF_CK       = 'CK'            # Clock
OBS_FF_Q        = 'Q'             # Output (unused, tied to net)
OBS_FF_QN       = 'QN'            # Complementary output (unused)

# Name of the global clock net in your netlist -- change if yours differs
CLOCK_NET       = 'clk'
RESET_NET       = 'reset_b'        # Active-low reset net name

# Scan enable net name in your design
SCAN_EN_NET     = 'scan_en'


# =============================================================================
# Read candidate list from CSV
# =============================================================================
def read_candidates(csv_file, n_itps):
    candidates = []
    with open(csv_file) as f:
        reader = csv.DictReader(f)
        for row in reader:
            candidates.append(row)
    return candidates[:n_itps]


# =============================================================================
# Find the net driven by an instance output pin
# Works for /Y, /Q, /QN, /CO, /S output pins
# =============================================================================
def find_driven_net(content, inst_name, pin='Y'):
    escaped = re.escape(inst_name)

    # Match full instance block (may span multiple lines)
    pattern = r'[A-Z][A-Z0-9_]*\s+' + escaped + r'\s*\((.*?)\)\s*;'
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        return None

    port_block = m.group(1)

    # Find .PIN(netname) in the port block
    pin_pattern = r'\.' + re.escape(pin) + r'\s*\(\s*([^)]+)\s*\)'
    y_match = re.search(pin_pattern, port_block)
    if y_match:
        return y_match.group(1).strip()

    return None


# =============================================================================
# Find input-port uses of a net (excludes .Y, .Q, .QN -- output ports)
# =============================================================================
OUTPUT_PORTS = {'Y', 'Q', 'QN', 'CO', 'S', 'SO', 'SN'}

def find_net_uses(content, net_name):
    escaped_net = re.escape(net_name)
    pattern = r'\.([A-Z][A-Z0-9_]*)\s*\(\s*' + escaped_net + r'\s*\)'
    matches = re.findall(pattern, content)
    return [p for p in matches if p not in OUTPUT_PORTS]


# =============================================================================
# Replace input-port connections from old_net to new_net
# Output port connections (.Y, .Q etc.) are NOT touched
# =============================================================================
def replace_net_in_inputs(content, old_net, new_net):
    escaped_old = re.escape(old_net)

    def replacer(m):
        port = m.group(1)
        return '.%s(%s)' % (port, new_net)

    pattern = r'\.([A-Z][A-Z0-9_]*)\s*\(\s*' + escaped_old + r'\s*\)'

    def safe_replacer(m):
        port = m.group(1)
        if port in OUTPUT_PORTS:
            return m.group(0)   # leave output ports unchanged
        return '.%s(%s)' % (port, new_net)

    return re.sub(pattern, safe_replacer, content)


# =============================================================================
# Add wire declarations after the last existing wire block
# =============================================================================
def add_wire_declarations(content, new_wires):
    if not new_wires:
        return content

    wire_decl = '\n  wire   ' + ', '.join(new_wires) + ';'

    last_wire = list(re.finditer(r'wire\s+[^;]+;', content))
    if last_wire:
        insert_pos = last_wire[-1].end()
        content = content[:insert_pos] + wire_decl + content[insert_pos:]
    else:
        content = content.replace('endmodule', wire_decl + '\nendmodule', 1)

    return content


# =============================================================================
# Build XOR control point instance string
# =============================================================================
def build_xor_instance(itp_num, orig_net, itp_net):
    return (
        '\n  // CP-%d : XOR inversion test point  (original net: %s)\n'
        '  %s U_CP_%d ( .%s(%s), .%s(1\'b1), .%s(%s) );'
    ) % (
        itp_num, orig_net,
        XOR_CELL, itp_num,
        XOR_PORT_A, orig_net,
        XOR_PORT_B,
        XOR_PORT_Y, itp_net
    )


# =============================================================================
# Build observation FF instance string
# The obs FF taps the target net at its D input.
# SI chains obs FFs together: scan_obs_in -> U_OP_1 -> U_OP_2 -> ... -> scan_obs_out
# =============================================================================
def build_obs_instance(itp_num, target_net, prev_q_net, clock_net, reset_net, se_net):
    q_net  = 'obs_q_%d'  % itp_num
    qn_net = 'obs_qn_%d' % itp_num

    return (
        '\n  // OP-%d : Observation point tap  (tapping net: %s)\n'
        '  %s U_OP_%d (\n'
        '    .%s(%s),\n'      # D  = target net
        '    .%s(%s),\n'      # SI = previous obs Q (or scan_obs_in for first)
        '    .%s(%s),\n'      # SE = scan enable
        '    .%s(%s),\n'      # CK = clock
        '    .%s(%s),\n'      # RSTB = reset
        '    .%s(%s),\n'      # Q
        '    .%s(%s)\n'       # QN
        '  );'
    ) % (
        itp_num, target_net,
        OBS_FF_CELL, itp_num,
        OBS_FF_D,    target_net,
        OBS_FF_SI,   prev_q_net,
        OBS_FF_SE,   se_net,
        OBS_FF_CK,   clock_net,
        OBS_FF_RSTB, reset_net,
        OBS_FF_Q,    q_net,
        OBS_FF_QN,   qn_net,
    ), q_net, qn_net


# =============================================================================
# Detect clock, reset, and scan_enable net names from the netlist
# Falls back to configured defaults if not found
# =============================================================================
def detect_global_nets(content):
    clk   = CLOCK_NET
    rst   = RESET_NET
    se    = SCAN_EN_NET

    # Try to find common clock nets used in FF instantiations
    clk_match = re.search(r'\.CK\s*\(\s*([^)]+)\s*\)', content)
    if clk_match:
        clk = clk_match.group(1).strip()

    rst_match = re.search(r'\.RSTB\s*\(\s*([^)]+)\s*\)', content)
    if rst_match:
        rst = rst_match.group(1).strip()

    se_match = re.search(r'\.SE\s*\(\s*([^)]+)\s*\)', content)
    if se_match:
        se = se_match.group(1).strip()

    return clk, rst, se


# =============================================================================
# Main insertion function
# =============================================================================
def insert_itps(netlist_in, candidates, n_itps, circuit_name, mode):

    print('\nReading netlist: %s' % netlist_in)
    with open(netlist_in) as f:
        content = f.read()

    clock_net, reset_net, se_net = detect_global_nets(content)
    print('  Detected clock  : %s' % clock_net)
    print('  Detected reset  : %s' % reset_net)
    print('  Detected scan_en: %s' % se_net)

    report_lines = [
        'ITP Insertion Report',
        'Circuit  : %s' % circuit_name,
        'Netlist  : %s' % netlist_in,
        'Mode     : %s' % mode.upper(),
        'N ITPs   : %d' % n_itps,
        'Clock    : %s' % clock_net,
        'Reset    : %s' % reset_net,
        'Scan en  : %s' % se_net,
        '=' * 70,
    ]

    new_wires     = []
    cp_instances  = []
    op_instances  = []
    op_wires      = []     # Q and QN wires for obs FFs
    inserted_cp   = []
    inserted_op   = []
    skipped       = []

    prev_q_net    = 'scan_obs_in'   # start of obs FF scan chain

    for i, cand in enumerate(candidates, 1):
        node       = cand['node'].strip()
        tp_type    = cand.get('type', 'CP').strip().upper()
        fault_type = cand.get('fault_type', '?')
        cc0        = cand.get('CC0', '?')
        cc1        = cand.get('CC1', '?')
        co         = cand.get('CO',  '?')
        score      = cand.get('score', '?')

        # Override type from mode flag if not mixed
        if mode == 'cp':
            tp_type = 'CP'
        elif mode == 'op':
            tp_type = 'OP'
        # else: use the 'type' column from the CSV (mixed mode)

        # Parse instance and pin from node  e.g.  U4097/Y  or  \DP_.../U27/Y
        node_parts = node.split('/')
        inst = '/'.join(node_parts[:-1]) if len(node_parts) > 1 else node
        pin  = node_parts[-1] if len(node_parts) > 1 else 'Y'

        print('  [%2d] %-4s  node: %-30s  pin: %s' % (i, tp_type, node, pin))

        # Only /Y output pins for CP; /Y, /Q, /QN for OP
        valid_cp_pins = {'Y'}
        valid_op_pins = {'Y', 'Q', 'QN', 'CO', 'S'}

        if tp_type == 'CP' and pin not in valid_cp_pins:
            msg = 'SKIPPED (CP requires /Y pin, got /%s): %s' % (pin, node)
            print('         ' + msg)
            skipped.append((node, tp_type, msg))
            report_lines.append('[%2d] %s' % (i, msg))
            continue

        if tp_type == 'OP' and pin not in valid_op_pins:
            msg = 'SKIPPED (OP unsupported pin /%s): %s' % (pin, node)
            print('         ' + msg)
            skipped.append((node, tp_type, msg))
            report_lines.append('[%2d] %s' % (i, msg))
            continue

        # Find the net driven by this pin
        target_net = find_driven_net(content, inst, pin)
        if not target_net:
            msg = 'SKIPPED (could not find driven net for %s/%s)' % (inst, pin)
            print('         ' + msg)
            skipped.append((node, tp_type, msg))
            report_lines.append('[%2d] %s' % (i, msg))
            continue

        # ------------------------------------------------------------------
        # CP insertion: XOR gate between original net and downstream logic
        # ------------------------------------------------------------------
        if tp_type == 'CP':
            itp_net = '%s_cp%d' % (re.sub(r'[\\[\]]', '_', target_net), i)

            uses = find_net_uses(content, target_net)
            print('         orig_net=%-15s  itp_net=%-20s  redirecting %d connections'
                  % (target_net, itp_net, len(uses)))

            content = replace_net_in_inputs(content, target_net, itp_net)
            new_wires.append(itp_net)
            cp_instances.append(build_xor_instance(i, target_net, itp_net))

            inserted_cp.append({
                'num': i, 'node': node, 'inst': inst, 'pin': pin,
                'orig_net': target_net, 'itp_net': itp_net,
                'uses': len(uses),
                'CC0': cc0, 'CC1': cc1, 'CO': co, 'score': score,
            })
            report_lines.append(
                '[%2d] CP INSERTED  node=%-25s  %s --> %s  (%d conn)  CC0=%s CC1=%s CO=%s score=%s'
                % (i, node, target_net, itp_net, len(uses), cc0, cc1, co, score)
            )

        # ------------------------------------------------------------------
        # OP insertion: scan observation FF taps the target net
        # The OP does NOT redirect any connections -- it only taps (reads)
        # ------------------------------------------------------------------
        elif tp_type == 'OP':
            print('         tapping  net=%-20s  prev_scan_q=%s'
                  % (target_net, prev_q_net))

            inst_str, q_net, qn_net = build_obs_instance(
                i, target_net, prev_q_net, clock_net, reset_net, se_net
            )
            op_instances.append(inst_str)
            op_wires.extend([q_net, qn_net])
            prev_q_net = q_net    # chain next OP to this Q

            inserted_op.append({
                'num': i, 'node': node, 'inst': inst, 'pin': pin,
                'target_net': target_net, 'q_net': q_net,
                'CC0': cc0, 'CC1': cc1, 'CO': co, 'score': score,
            })
            report_lines.append(
                '[%2d] OP INSERTED  node=%-25s  tapping=%s  q=%s  CC0=%s CC1=%s CO=%s score=%s'
                % (i, node, target_net, q_net, cc0, cc1, co, score)
            )

    # -----------------------------------------------------------------------
    # Add all new wires
    # -----------------------------------------------------------------------
    all_new_wires = new_wires + op_wires
    if op_instances:
        # Also add the scan_obs_in and scan_obs_out wires
        all_new_wires += ['scan_obs_in', 'scan_obs_out']
    content = add_wire_declarations(content, all_new_wires)

    # -----------------------------------------------------------------------
    # Build the ITP block and insert before endmodule
    # -----------------------------------------------------------------------
    itp_block_lines = [
        '',
        '  // ================================================================',
        '  // Auto-inserted Test Points  (%d CP, %d OP)' % (
            len(inserted_cp), len(inserted_op)),
        '  // Generated by insert_itp.py',
        '  // ================================================================',
    ]

    if cp_instances:
        itp_block_lines.append('\n  // --- Control Points (XOR inversion) ---')
        itp_block_lines.extend(cp_instances)

    if op_instances:
        itp_block_lines.append('\n  // --- Observation Points (scan tap FFs) ---')
        itp_block_lines.extend(op_instances)
        # Last Q in chain drives scan_obs_out (connect to your scan chain)
        itp_block_lines.append(
            '\n  // scan_obs_out carries the observation chain to your scan mux'
        )
        itp_block_lines.append(
            '  assign scan_obs_out = %s;' % prev_q_net
        )

    itp_block_lines.append(
        '\n  // ================================================================'
    )

    itp_block = '\n'.join(itp_block_lines)
    content = content.replace('\nendmodule', itp_block + '\nendmodule', 1)

    # -----------------------------------------------------------------------
    # Write output netlist
    # -----------------------------------------------------------------------
    netlist_out = '%s_itp%d.v' % (circuit_name, n_itps)
    with open(netlist_out, 'w') as f:
        f.write(content)

    # -----------------------------------------------------------------------
    # Write report
    # -----------------------------------------------------------------------
    report_lines += [
        '=' * 70,
        'SUMMARY',
        '  Control points inserted  : %d' % len(inserted_cp),
        '  Observation pts inserted : %d' % len(inserted_op),
        '  Skipped                  : %d' % len(skipped),
        '',
    ]

    if inserted_cp:
        report_lines.append('CONTROL POINTS:')
        for r in inserted_cp:
            report_lines.append(
                '  CP-%2d  %-25s  orig=%-15s  itp=%-20s  CC0=%-4s CC1=%-4s CO=%-4s score=%s'
                % (r['num'], r['node'], r['orig_net'], r['itp_net'],
                   r['CC0'], r['CC1'], r['CO'], r['score'])
            )

    if inserted_op:
        report_lines.append('\nOBSERVATION POINTS:')
        for r in inserted_op:
            report_lines.append(
                '  OP-%2d  %-25s  tap=%-20s  q=%-15s  CC0=%-4s CC1=%-4s CO=%-4s score=%s'
                % (r['num'], r['node'], r['target_net'], r['q_net'],
                   r['CC0'], r['CC1'], r['CO'], r['score'])
            )

    if skipped:
        report_lines.append('\nSKIPPED:')
        for node, tp_type, reason in skipped:
            report_lines.append('  %-4s  %-25s  %s' % (tp_type, node, reason))

    if inserted_op:
        report_lines.append('\nSCAN CHAIN NOTE:')
        report_lines.append(
            '  Connect scan_obs_in  to your existing scan chain output (or tie to 1\'b0 for standalone).'
        )
        report_lines.append(
            '  Connect scan_obs_out to the next element in your scan chain or primary output.'
        )
        report_lines.append(
            '  Total observation FFs added: %d  (adds %d scan shift cycles)'
            % (len(inserted_op), len(inserted_op))
        )

    report_out = '%s_itp%d_report.txt' % (circuit_name, n_itps)
    with open(report_out, 'w') as f:
        f.write('\n'.join(report_lines) + '\n')

    # Print summary
    print('\n' + '=' * 70)
    print('  DONE')
    print('  Control points   : %d inserted' % len(inserted_cp))
    print('  Observation pts  : %d inserted' % len(inserted_op))
    print('  Skipped          : %d nodes'    % len(skipped))
    print('  Output netlist   : %s'           % netlist_out)
    print('  Report           : %s'           % report_out)
    if inserted_op:
        print('\n  ACTION REQUIRED: Connect scan_obs_in / scan_obs_out')
        print('  to your existing scan chain in the top-level netlist.')
    print('=' * 70 + '\n')

    return netlist_out, report_out, inserted_cp, inserted_op, skipped


# =============================================================================
# Entry point
# =============================================================================
def main():
    if len(sys.argv) < 4:
        print('Usage: python insert_itp.py <netlist.v> <candidates.csv> <n_itps> [--mode cp|op|mixed]')
        print('')
        print('Examples:')
        print('  python insert_itp.py b15_scan.v b15_cp_top15.csv       10 --mode cp')
        print('  python insert_itp.py b15_scan.v b15_op_top15.csv       10 --mode op')
        print('  python insert_itp.py b15_scan.v b15_combined_top15.csv 15 --mode mixed')
        print('  python insert_itp.py b14_scan.v b14_cp_top15.csv        5 --mode cp')
        print('  python insert_itp.py b17_scan.v b17_combined_top15.csv 15 --mode mixed')
        sys.exit(1)

    netlist_in = sys.argv[1]
    csv_file   = sys.argv[2]
    n_itps     = int(sys.argv[3])

    # Parse optional --mode flag
    mode = 'mixed'
    if '--mode' in sys.argv:
        idx = sys.argv.index('--mode')
        if idx + 1 < len(sys.argv):
            mode = sys.argv[idx + 1].lower()
    if mode not in ('cp', 'op', 'mixed'):
        print('ERROR: --mode must be cp, op, or mixed')
        sys.exit(1)

    if not os.path.exists(netlist_in):
        print('ERROR: Netlist not found -- %s' % netlist_in)
        sys.exit(1)
    if not os.path.exists(csv_file):
        print('ERROR: CSV not found -- %s' % csv_file)
        sys.exit(1)

    circuit_name = os.path.splitext(os.path.basename(netlist_in))[0]

    print('')
    print('=' * 70)
    print('  ITP Insertion')
    print('  Circuit  : %s' % circuit_name)
    print('  Netlist  : %s' % netlist_in)
    print('  CSV      : %s' % csv_file)
    print('  N ITPs   : %d' % n_itps)
    print('  Mode     : %s' % mode.upper())
    print('=' * 70)

    candidates = read_candidates(csv_file, n_itps)
    print('\nLoaded %d candidates from %s:' % (len(candidates), csv_file))
    for i, c in enumerate(candidates, 1):
        print('  %2d. %-4s  %-30s  CC0=%-4s CC1=%-4s CO=%s'
              % (i,
                 c.get('type', 'CP'),
                 c['node'],
                 c.get('CC0', '?'),
                 c.get('CC1', '?'),
                 c.get('CO',  '?')))

    insert_itps(netlist_in, candidates, n_itps, circuit_name, mode)


if __name__ == '__main__':
    main()
