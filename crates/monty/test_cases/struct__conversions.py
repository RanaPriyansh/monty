import struct

events = []


class IndexValue:
    def __index__(self):
        events.append('index')
        return 18446744073709551615


class FloatValue:
    def __float__(self):
        events.append('float')
        return 1.25

    def __index__(self):
        assert False, '__float__ must take precedence'


class ComplexValue:
    def __complex__(self):
        events.append('complex')
        return 1 + 2j

    def __float__(self):
        assert False, '__complex__ must take precedence'


assert struct.pack('>QdD', IndexValue(), FloatValue(), ComplexValue()) == bytes.fromhex(
    'ffffffffffffffff3ff40000000000003ff00000000000004000000000000000'
)
assert events == ['index', 'float', 'complex']


class BrokenFloat:
    def __float__(self):
        raise ValueError('from float')


class BrokenComplex:
    def __complex__(self):
        return []


class BrokenIndex:
    def __index__(self):
        raise ValueError('from index')


for code, value, message in [
    ('d', BrokenFloat(), 'required argument is not a float'),
    ('D', BrokenComplex(), 'required argument is not a complex'),
]:
    try:
        struct.pack(code, value)
        assert False, 'conversion must fail'
    except struct.error as error:
        assert str(error) == message

try:
    struct.pack('I', BrokenIndex())
    assert False, 'integer conversion must preserve ValueError'
except ValueError as error:
    assert str(error) == 'from index'

for format, message in [
    ('Z', 'bad char in struct format'),
    ('2', 'repeat count given without format specifier'),
    ('2 i', 'bad char in struct format'),
    ('1_000_000i', 'bad char in struct format'),
    ('i\x00', 'embedded null character'),
    ('999999999999999999999999999999999999x', 'total struct size too long'),
]:
    try:
        struct.calcsize(format)
        assert False, 'format must fail'
    except struct.error as error:
        assert str(error) == message

for format in ['=n', '<N', '>P', '!n']:
    try:
        struct.calcsize(format)
        assert False, 'native-only code must fail'
    except struct.error as error:
        assert str(error) == 'bad char in struct format'

try:
    struct.unpack('1000000000B', b'')
    assert False, 'buffer check must precede result allocation'
except struct.error as error:
    assert str(error) == 'unpack requires a buffer of 1000000000 bytes'

assert events == ['index', 'float', 'complex']
