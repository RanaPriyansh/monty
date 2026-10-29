import struct

payload = b'\x00\x00\x02\x80\x00\x00\x01\xe0'
assert struct.calcsize('>II') == 8
assert struct.unpack('>II', payload) == (640, 480)
assert struct.pack('>II', 640, 480) == payload
