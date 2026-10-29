payload = bytes(b'\x00\x00\x02\x80')
assert payload == b'\x00\x00\x02\x80'
assert payload[2:4] == b'\x02\x80'
assert payload[3] == 128
