import struct

half = 10**15


class WideIndex:
    def __index__(self):
        return half * half


class ComplexResult:
    def __complex__(self):
        return complex(half, half)


class BrokenFloat:
    def __float__(self):
        return ['temporary']


for _ in range(3):
    try:
        struct.pack('>Q', WideIndex())
        assert False, 'wide conversion must fail'
    except struct.error:
        pass
    try:
        struct.pack('>Dd', ComplexResult(), BrokenFloat())
        assert False, 'later conversion must fail'
    except struct.error:
        pass
    assert struct.unpack('>QcD', struct.pack('>QcD', 18446744073709551615, b'x', 1 + 2j)) == (
        18446744073709551615,
        b'x',
        1 + 2j,
    )

len('done')
# ref-counts={'struct': 1, 'WideIndex': 1, 'ComplexResult': 1, 'BrokenFloat': 1}
