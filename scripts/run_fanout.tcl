# Read node list from CSV and run report_fanout for each
# Works for any circuit -- reads from itp_selection_summary.csv

# ── Configuration ─────────────────────────────────────
# Change these two lines for each circuit
set circuit    "b14"
set csv_file   "b14_method1_raw_top15.csv"
set out_file   "fanout_${circuit}.rpt"
set n_itps     15

# ── Read CSV and extract nodes ─────────────────────────
set fp [open $csv_file r]
set lines [split [read $fp] "\n"]
close $fp

# Skip header line
set header [lindex $lines 0]
set data_lines [lrange $lines 1 end]

# Find which column is 'node'
set col_names [split $header ","]
set node_col  [lsearch $col_names "node"]

# Extract node names
set nodes {}
foreach line $data_lines {
    if {[string trim $line] == ""} continue
    set cols [split $line ","]
    set node [string trim [lindex $cols $node_col]]
    if {$node != ""} {
        lappend nodes $node
    }
}

# Limit to n_itps
set nodes [lrange $nodes 0 [expr {$n_itps - 1}]]

# ── Run report_fanout for each node ───────────────────
# Clear output file first
set fp [open $out_file w]
puts $fp "Fanout report for circuit: $circuit"
puts $fp "Generated from: $csv_file"
puts $fp "Number of ITP nodes: $n_itps"
puts $fp "================================================"
close $fp

foreach node $nodes {
    # Only process output pins /Y
    if {[string match "*\/Y" $node]} {
        puts "Processing: $node"
        redirect -append -file $out_file {
            puts "------------------------------------------------"
            puts "Node: $node"
            report_fanout -from $node
        }
    } else {
        puts "Skipping input pin: $node"
        set fp [open $out_file a]
        puts $fp "------------------------------------------------"
        puts $fp "SKIPPED (input pin): $node"
        close $fp
    }
}

puts ""
puts "Done -- fanout report saved to: $out_file"
