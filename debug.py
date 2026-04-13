python -c "
import re
line = 'sa1   NC   U1503/Y   (INVX8_LVT)   ( 1: 52/1/0, SCOAP=1/1/1 0/0/0/0 )'
m1 = re.match(r'(sa[01])\s+(NC|NO)\s+(\S+)', line)
print('Part1 fault+node:', 'OK' if m1 else 'FAIL')
m2 = re.search(r'\((\S+)\)', line)
print('Part2 cell:', m2.group(1) if m2 else 'FAIL')
m3 = re.search(r'SCOAP=(\d+)/(\d+)/(\d+)', line)
print('Part3 SCOAP:', m3.group(0) if m3 else 'FAIL')
m4 = re.search(r'(\d+)/(\d+)/(\d+)\s*\)', line)
print('Part4 seq+close:', m4.group(0) if m4 else 'FAIL')
"
