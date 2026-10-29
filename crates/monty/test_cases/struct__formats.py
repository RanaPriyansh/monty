import struct

assert struct.pack(
    '>bBhHiIlLqQ',
    -128,
    255,
    -32768,
    65535,
    -2147483648,
    4294967295,
    -2147483648,
    4294967295,
    -9223372036854775808,
    18446744073709551615,
) == bytes.fromhex('80ff8000ffff80000000ffffffff80000000ffffffff8000000000000000ffffffffffffffff')
assert struct.unpack('>qQ', bytes.fromhex('8000000000000000ffffffffffffffff')) == (
    -9223372036854775808,
    18446744073709551615,
)
assert struct.pack('<2H', 1, 256) == b'\x01\x00\x00\x01'
assert struct.pack('!2H', 1, 256) == b'\x00\x01\x01\x00'
assert struct.pack('>2c3s5p2x?', b'a', b'b', b'cdef', b'ghijkl', []) == b'abcde\x04ghij\x00\x00\x00'
assert struct.unpack('>2c3s5p2x?', b'abcde\x04ghij\x00\x00\x7f') == (b'a', b'b', b'cde', b'ghij', True)
assert struct.pack('0s0p', b'abc', b'def') == b''
assert struct.unpack('0s0p', b'') == (b'', b'')
assert struct.pack('1p', b'hello') == b'\x00'
assert struct.unpack('5p', b'\xffabcd') == (b'abcd',)
assert struct.pack('300p', b'x' * 299) == b'\xff' + b'x' * 299
assert struct.unpack('300p', b'\xff' + b'x' * 299) == (b'x' * 255,)
assert struct.calcsize(' 2h\t3B\n') == struct.calcsize('2h3B')
assert struct.calcsize('1000000000B') == 1000000000
assert struct.calcsize('0Q') == 0
assert struct.calcsize('b0Q') == struct.calcsize('bQ') - struct.calcsize('Q')
assert struct.calcsize('=b0Q') == 1
assert struct.pack(b'>I', 640) == b'\x00\x00\x02\x80'
assert struct.calcsize(b'>I') == 4
assert struct.pack('>e', 1.00048828125) == b'\x3c\x00'
assert struct.pack('>e', 1.0004882812500002) == b'\x3c\x01'
assert struct.pack('>e', -0.0) == b'\x80\x00'
assert struct.pack('>e', 2**-24) == b'\x00\x01'
assert struct.unpack('>e', b'\x00\x01') == (2**-24,)
assert struct.pack('>fd', 1.25, -2.5) == bytes.fromhex('3fa00000c004000000000000')
assert struct.pack('>FD', 1 + 2j, -3 + 4j) == bytes.fromhex('3f80000040000000c0080000000000004010000000000000')
assert struct.unpack('>FD', struct.pack('>FD', 1 + 2j, -3 + 4j)) == (1 + 2j, -3 + 4j)
assert struct.unpack('@N', struct.pack('@N', 12345)) == (12345,)
assert struct.unpack('@n', struct.pack('@n', -12345)) == (-12345,)
assert struct.unpack('@P', struct.pack('@P', -1)) == (2 ** (struct.calcsize('@P') * 8) - 1,)
assert issubclass(struct.error, Exception)
assert not issubclass(struct.error, ValueError), 'struct.error must derive directly from Exception'
