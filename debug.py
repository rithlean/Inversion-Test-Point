python -c "
import re
line = 'sa1   NC   U1503/Y   (INVX8_LVT)   ( 1: 52/1/0, SCOAP=1/1/1 0/0/0/0 )'
m = re.match(
    r'(sa[01])\s+(NC|NO)\s+(\S+)\s+\(([^)]+)\)\s+\([^,]+,\s*SCOAP=(\d+)/(\d+)/(\d+)\s+\d+/\d+/\d+\s*\)',
    line
)
if m:
    print('MATCH')
    print('node:', m.group(3))
    print('cell:', m.group(4))
    print('CC0:', m.group(5))
    print('CC1:', m.group(6))
    print('CO:', m.group(7))
else:
    print('FAIL')
"
