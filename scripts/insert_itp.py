# -*- coding: utf-8 -*-
# =============================================================================
# Universal XOR Inversion Test Point (ITP) Insertion Script
# =============================================================================
# Usage:
#   python insert_itp.py <netlist.v> <candidates.csv> <n_itps>
#
# Examples:
#   python insert_itp.py b14_scan.v b14_method1_raw_top15.csv 5
#   python insert_itp.py b15_scan.v b15_method2_weighted_top15.csv 10
#   python insert_itp.py b17_scan.v b17_method1_raw_top15.csv 15
#
# What it does:
#   1. Reads top-N ITP candidate nodes from the CSV
#   2. For each node (e.g. U2526/Y), finds the net it drives (e.g. n2526)
#   3. Inserts a XOR2X1_LVT gate: output = net XOR 1'b1 (always inverting)
#   4. Replaces all downstream uses of original net with the ITP net
#   5. Adds new wire declarations
#   6. Writes modified netlist to <circuit>_itp<N>.v
#   7. Writes insertion report to <circuit>_itp<N>_report.txt
# =============================================================================

import re
import csv
import sys
import os

# =============================================================================
# Configuration -- adjust if your library uses different cell/port names
# =============================================================================
XOR_CELL   = 'XOR2X1_LVT'   # XOR gate cell name in your library
XOR_PORT_A = 'A1'            # First input port of XOR cell
XOR_PORT_B = 'A2'            # Second input port of XOR cell (gets tied to 1)
XOR_PORT_Y = 'Y'             # Output port of XOR cell


# =============================================================================
# Step 1 -- Read candidate nodes from CSV
# =============================================================================
def read_candidates(csv_file, n_itps):
    candidates = []
    with open(csv_file) as f:
        reader = csv.DictReader(f)
        for row in reader:
            candidates.append(row)
    return candidates[:n_itps]


# =============================================================================
# Step 2 -- Find the net name driven by each ITP node
#
# In a DC-synthesized netlist like b14:
#   Gate U2526 drives net n2526
#   Gate U2162 drives net n2162
#   The pattern is: CELLTYPE Uxxxx ( ... .Y(netname) ... )
#
# This function finds the .Y(netname) of a given instance
# =============================================================================
def find_driven_net(content, inst_name):
    # Escape backslash for regex (escaped instance names like \reg0_reg[0])
    escaped = re.escape(inst_name)

    # Match the full instance block
    # Pattern: CELLTYPE inst_name ( ... .Y(netname) ... );
    # The instance may span multiple lines
    pattern = r'[A-Z][A-Z0-9_]*\s+' + escaped + r'\s*\((.*?)\)\s*;'
    m = re.search(pattern, content, re.DOTALL)

    if not m:
        return None

    port_block = m.group(1)

    # Find .Y(netname) in the port block
    y_match = re.search(r'\.Y\s*\(\s*([^)]+)\s*\)', port_block)
    if y_match:
        return y_match.group(1).strip()

    return None


# =============================================================================
# Step 3 -- Find all instances using a given net as an INPUT port
#
# We look for .(portname)(netname) where portname is NOT .Y
# This finds all gates that consume the net
# =============================================================================
def find_net_uses(content, net_name):
    # Escape net name for regex (handles n2526, \reg0_reg[0] etc)
    escaped_net = re.escape(net_name)

    # Match any input port connection using this net
    # Pattern: .(portname)(net_name)
    # Excludes .Y (output) -- we only want input uses
    pattern = r'\.((?!Y\b)[A-Z][A-Z0-9_]*)\s*\(\s*' + escaped_net + r'\s*\)'

    matches = re.findall(pattern, content)
    return matches


# =============================================================================
# Step 4 -- Replace net uses in content
#
# Replaces all input port connections from old_net to new_net
# Carefully avoids replacing the .Y(old_net) driving assignment
# =============================================================================
def replace_net_in_inputs(content, old_net, new_net, driving_inst):
    escaped_old = re.escape(old_net)
    escaped_drv = re.escape(driving_inst)

    # Replace .(inputport)(old_net) with .(inputport)(new_net)
    # but NOT .Y(old_net) which is the driver
    def replacer(m):
        port = m.group(1)
        return '.%s(%s)' % (port, new_net)

    pattern = r'\.((?!Y\b)[A-Z][A-Z0-9_]*)\s*\(\s*' + escaped_old + r'\s*\)'
    new_content = re.sub(pattern, replacer, content)

    return new_content


# =============================================================================
# Step 5 -- Add wire declarations to the wire block
# =============================================================================
def add_wire_declarations(content, new_wires):
    if not new_wires:
        return content

    wire_decl = '\n  wire   ' + ', '.join(new_wires) + ';'

    # Find the last wire declaration line and insert after it
    # Look for the end of the existing wire block (before assign or first instance)
    last_wire = list(re.finditer(r'wire\s+[^;]+;', content))
    if last_wire:
        insert_pos = last_wire[-1].end()
        content = content[:insert_pos] + wire_decl + content[insert_pos:]
    else:
        # Fallback: insert before endmodule
        content = content.replace('endmodule', wire_decl + '\nendmodule', 1)

    return content


# =============================================================================
# Step 6 -- Build XOR ITP instance string
# =============================================================================
def build_xor_instance(itp_num, orig_net, itp_net):
    lines = [
        '',
        '  // XOR ITP %d -- original net: %s' % (itp_num, orig_net),
        '  %s U_ITP_%d ( .%s(%s), .%s(1\'b1), .%s(%s) );' % (
            XOR_CELL, itp_num,
            XOR_PORT_A, orig_net,
            XOR_PORT_B,
            XOR_PORT_Y, itp_net
        ),
    ]
    return '\n'.join(lines)


# =============================================================================
# Main insertion function
# =============================================================================
def insert_xor_itp(netlist_in, candidates, n_itps, circuit_name):

    print('\nReading netlist: %s' % netlist_in)
    with open(netlist_in) as f:
        content = f.read()

    report_lines = []
    report_lines.append('XOR ITP Insertion Report')
    report_lines.append('Circuit  : %s' % circuit_name)
    report_lines.append('Netlist  : %s' % netlist_in)
    report_lines.append('N ITPs   : %d' % n_itps)
    report_lines.append('=' * 60)

    new_wires     = []
    xor_instances = []
    inserted      = []
    skipped       = []

    for i, cand in enumerate(candidates, 1):
        node     = cand['node'].strip()
        cc0      = cand.get('CC0', '?')
        cc1      = cand.get('CC1', '?')
        co       = cand.get('CO',  '?')
        score    = cand.get('raw_score', cand.get('weighted_score', '?'))

        # Parse instance name and pin
        # e.g. U2526/Y  ->  inst=U2526, pin=Y
        parts = node.split('/')
        inst  = parts[0]
        pin   = parts[1] if len(parts) > 1 else 'Y'

        print('  [%2d] Processing node: %s' % (i, node))

        # Only handle output pins
        if pin != 'Y':
            msg = 'SKIPPED (input pin -- only /Y supported): %s' % node
            print('       ' + msg)
            skipped.append((node, msg))
            report_lines.append('[%2d] %s' % (i, msg))
            continue

        # Find the net driven by this instance
        orig_net = find_driven_net(content, inst)
        if not orig_net:
            msg = 'SKIPPED (could not find driven net for instance): %s' % inst
            print('       ' + msg)
            skipped.append((node, msg))
            report_lines.append('[%2d] %s' % (i, msg))
            continue

        # Create ITP net name
        itp_net = '%s_itp%d' % (orig_net, i)

        print('       Original net : %s' % orig_net)
        print('       ITP net      : %s' % itp_net)

        # Count how many uses will be replaced
        uses = find_net_uses(content, orig_net)
        print('       Input uses   : %d connections will be redirected' % len(uses))

        # Replace net in all input port connections
        content = replace_net_in_inputs(content, orig_net, itp_net, inst)

        # Collect new wire and XOR instance
        new_wires.append(itp_net)
        xor_instances.append(build_xor_instance(i, orig_net, itp_net))

        inserted.append({
            'num'      : i,
            'node'     : node,
            'inst'     : inst,
            'orig_net' : orig_net,
            'itp_net'  : itp_net,
            'uses'     : len(uses),
            'CC0'      : cc0,
            'CC1'      : cc1,
            'CO'       : co,
            'score'    : score,
        })

        report_lines.append('[%2d] INSERTED  node=%-20s  net: %s --> %s  (%d connections)  CC0=%s CC1=%s CO=%s' % (
            i, node, orig_net, itp_net, len(uses), cc0, cc1, co
        ))

    # Add wire declarations
    content = add_wire_declarations(content, new_wires)

    # Build XOR block and insert before endmodule
    itp_block = (
        '\n\n'
        '  // ================================================================\n'
        '  // XOR Inversion Test Points -- auto-inserted (%d ITPs)\n'
        '  // itp_ctrl tied to 1\'b1 (always active in test mode)\n'
        '  // ================================================================'
        % len(inserted)
    )
    itp_block += ''.join(xor_instances)
    itp_block += '\n  // ================================================================\n'

    content = content.replace('\nendmodule', itp_block + '\nendmodule', 1)

    # Write output netlist
    netlist_out = '%s_itp%d.v' % (circuit_name, n_itps)
    with open(netlist_out, 'w') as f:
        f.write(content)

    # Write report
    report_lines.append('=' * 60)
    report_lines.append('SUMMARY')
    report_lines.append('  Successfully inserted : %d ITPs' % len(inserted))
    report_lines.append('  Skipped               : %d nodes' % len(skipped))
    report_lines.append('')
    report_lines.append('INSERTED NODES (ranked by score):')
    for r in inserted:
        report_lines.append('  ITP %2d: %-20s  orig_net=%-10s  itp_net=%-15s  CC0=%-5s CC1=%-5s CO=%-5s score=%s' % (
            r['num'], r['node'], r['orig_net'], r['itp_net'],
            r['CC0'], r['CC1'], r['CO'], r['score']
        ))

    if skipped:
        report_lines.append('')
        report_lines.append('SKIPPED NODES:')
        for node, reason in skipped:
            report_lines.append('  %s -- %s' % (node, reason))

    report_out = '%s_itp%d_report.txt' % (circuit_name, n_itps)
    with open(report_out, 'w') as f:
        f.write('\n'.join(report_lines) + '\n')

    # Print summary
    print('\n' + '=' * 60)
    print('  DONE')
    print('  Inserted  : %d XOR ITPs' % len(inserted))
    print('  Skipped   : %d nodes' % len(skipped))
    print('  Output    : %s' % netlist_out)
    print('  Report    : %s' % report_out)
    print('=' * 60 + '\n')

    return netlist_out, report_out, inserted, skipped


# =============================================================================
# Entry point
# =============================================================================
def main():
    if len(sys.argv) < 4:
        print('Usage: python insert_itp.py <netlist.v> <candidates.csv> <n_itps>')
        print('')
        print('Examples:')
        print('  python insert_itp.py b14_scan.v b14_method1_raw_top15.csv 5')
        print('  python insert_itp.py b15_scan.v b15_method2_weighted_top15.csv 10')
        print('  python insert_itp.py b17_scan.v b17_method1_raw_top15.csv 15')
        sys.exit(1)

    netlist_in = sys.argv[1]
    csv_file   = sys.argv[2]
    n_itps     = int(sys.argv[3])

    # Validate inputs
    if not os.path.exists(netlist_in):
        print('ERROR: Netlist not found -- %s' % netlist_in)
        sys.exit(1)

    if not os.path.exists(csv_file):
        print('ERROR: CSV not found -- %s' % csv_file)
        sys.exit(1)

    # Derive circuit name from netlist filename
    # e.g. b14_scan.v --> b14_scan
    circuit_name = os.path.splitext(os.path.basename(netlist_in))[0]

    print('')
    print('=' * 60)
    print('  XOR ITP Insertion')
    print('  Circuit  : %s' % circuit_name)
    print('  Netlist  : %s' % netlist_in)
    print('  CSV      : %s' % csv_file)
    print('  N ITPs   : %d' % n_itps)
    print('=' * 60)

    # Read candidates
    candidates = read_candidates(csv_file, n_itps)
    print('\nTop %d candidates loaded from %s:' % (len(candidates), csv_file))
    for i, c in enumerate(candidates, 1):
        print('  %2d. %-20s  CC0=%-5s CC1=%-5s CO=%s' % (
            i, c['node'], c.get('CC0','?'), c.get('CC1','?'), c.get('CO','?')
        ))

    # Run insertion
    insert_xor_itp(netlist_in, candidates, n_itps, circuit_name)


if __name__ == '__main__':
    main()
