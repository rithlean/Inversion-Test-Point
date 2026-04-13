python -c "
import re
line = 'sa1   NC   U1503/Y   (INVX8_LVT)   ( 1: 52/1/0, SCOAP=1/1/1 0/0/0/0 )'
print('repr:', repr(line))
parts = line.split()
print('parts:', parts)
"
