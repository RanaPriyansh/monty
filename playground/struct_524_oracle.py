from __future__ import annotations

import ctypes
import json
import math
import platform
import struct
import sys
from typing import Any


PREFIXES = ('', '@', '=', '<', '>', '!')
FORMAT_CODES = tuple('xcbB?hHiIlLqQnNefdFDspP')
VALUES = {
    'x': (),
    'c': (b'A',),
    'b': (-7,),
    'B': (7,),
    '?': (True,),
    'h': (-1234,),
    'H': (1234,),
    'i': (-123456,),
    'I': (123456,),
    'l': (-123456,),
    'L': (123456,),
    'q': (-123456789,),
    'Q': (123456789,),
    'n': (-123456,),
    'N': (123456,),
    'e': (1.25,),
    'f': (1.25,),
    'd': (1.25,),
    'F': (1 + 2j,),
    'D': (1 + 2j,),
    's': (b'xy',),
    'p': (b'xy',),
    'P': (1,),
}


def _float_source(value: float) -> str:
    if math.isnan(value):
        return "float('nan')"
    if math.isinf(value):
        return "float('-inf')" if value < 0 else "float('inf')"
    return repr(value)


def _literal(value: object) -> str:
    if isinstance(value, float):
        return _float_source(value)
    return repr(value)


def _normalize(value: object) -> object:
    if isinstance(value, bytes):
        return {'bytes_hex': value.hex()}
    if isinstance(value, complex):
        return {'complex': [_normalize(value.real), _normalize(value.imag)]}
    if isinstance(value, float):
        if math.isnan(value):
            return {'float': 'nan'}
        if math.isinf(value):
            return {'float': '-inf' if value < 0 else 'inf'}
        return {'float_hex': value.hex()}
    if isinstance(value, (tuple, list)):
        return [_normalize(item) for item in value]
    return value


def _evaluate(
    setup: str,
    expression: str,
    trace_name: str | None,
    float64_inputs_be: dict[str, str],
) -> dict[str, object]:
    namespace: dict[str, object] = {'struct': struct}
    namespace.update(
        {
            name: struct.unpack('>d', bytes.fromhex(payload))[0]
            for name, payload in float64_inputs_be.items()
        }
    )
    try:
        if setup:
            exec(setup, namespace)
        value = eval(expression, namespace)
    except Exception as error:
        outcome: dict[str, object] = {
            'kind': 'error',
            'type': type(error).__name__,
            'message': str(error),
        }
    else:
        outcome = {'kind': 'value', 'value': _normalize(value)}
    if trace_name is not None and trace_name in namespace:
        outcome['trace'] = _normalize(namespace[trace_name])
    return outcome


def _add_case(
    records: list[dict[str, object]],
    name: str,
    expression: str,
    *,
    setup: str = '',
    trace_name: str | None = None,
    format_value: str | bytes | None = None,
    float64_inputs_be: dict[str, str] | None = None,
) -> None:
    float64_inputs_be = float64_inputs_be or {}
    namespace: dict[str, object] = {
        name: struct.unpack('>d', bytes.fromhex(payload))[0]
        for name, payload in float64_inputs_be.items()
    }
    decoded_inputs_be = {
        name: struct.pack('>d', value).hex() for name, value in namespace.items()
    }
    records.append(
        {
            'name': name,
            'format_repr': repr(format_value) if format_value is not None else None,
            'setup': setup,
            'expression': expression,
            'trace_name': trace_name,
            'float64_inputs_be': float64_inputs_be,
            'float64_inputs_after_decode_be': decoded_inputs_be,
            'expected': _evaluate(setup, expression, trace_name, float64_inputs_be),
        }
    )


def _pack_round_trip(format_value: str | bytes, values: tuple[object, ...]) -> str:
    arguments = ''.join(f', {_literal(value)}' for value in values)
    packed = f'struct.pack({format_value!r}{arguments})'
    return f'({packed}, struct.unpack({format_value!r}, {packed}))'


def _native_layout() -> dict[str, object]:
    class FloatComplex(ctypes.Structure):
        _fields_ = [('real', ctypes.c_float), ('imag', ctypes.c_float)]

    class DoubleComplex(ctypes.Structure):
        _fields_ = [('real', ctypes.c_double), ('imag', ctypes.c_double)]

    ctypes_types: dict[str, Any] = {
        'c': ctypes.c_char,
        'b': ctypes.c_byte,
        'B': ctypes.c_ubyte,
        '?': ctypes.c_bool,
        'h': ctypes.c_short,
        'H': ctypes.c_ushort,
        'i': ctypes.c_int,
        'I': ctypes.c_uint,
        'l': ctypes.c_long,
        'L': ctypes.c_ulong,
        'q': ctypes.c_longlong,
        'Q': ctypes.c_ulonglong,
        'n': ctypes.c_ssize_t,
        'N': ctypes.c_size_t,
        'e': ctypes.c_ushort,
        'f': ctypes.c_float,
        'd': ctypes.c_double,
        'F': FloatComplex,
        'D': DoubleComplex,
        'P': ctypes.c_void_p,
    }
    layout: dict[str, object] = {}
    for code in FORMAT_CODES:
        if code == 'x':
            continue
        size = struct.calcsize(f'@{code}')
        prefixed_size = struct.calcsize(f'@c{code}')
        alignment = prefixed_size - struct.calcsize('@c') - size + 1
        ctype = ctypes_types.get(code)
        layout[code] = {
            'struct_size': size,
            'struct_alignment_from_prefix_padding': alignment,
            'ctypes_size': ctypes.sizeof(ctype) if ctype is not None else None,
            'ctypes_alignment': ctypes.alignment(ctype) if ctype is not None else None,
        }
    return layout


def _environment() -> dict[str, object]:
    environment: dict[str, object] = {
        'implementation': platform.python_implementation(),
        'version': sys.version,
        'version_info': list(sys.version_info[:3]),
        'executable': sys.executable,
        'struct_module': struct.__file__,
        'byteorder': sys.byteorder,
    }
    if environment['implementation'] != 'CPython' or sys.version_info[:2] != (3, 14):
        return environment
    environment['native_layout'] = _native_layout()
    environment['endianness_samples'] = {
        prefix or 'default': struct.pack(f'{prefix}H', 0x1234).hex()
        for prefix in PREFIXES
    }
    return environment


def _records() -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for prefix in PREFIXES:
        for code in FORMAT_CODES:
            format_value = f'{prefix}{code}'
            _add_case(
                records,
                f'calcsize:{format_value!r}',
                f'struct.calcsize({format_value!r})',
                format_value=format_value,
            )
            _add_case(
                records,
                f'pack-unpack:{format_value!r}',
                _pack_round_trip(format_value, VALUES[code]),
                format_value=format_value,
            )

    half_values = (
        ('tie_even_down', 1.00048828125),
        ('tie_even_up', 1.00146484375),
        ('above_tie', math.nextafter(1.00048828125, math.inf)),
        ('below_tie', math.nextafter(1.00048828125, -math.inf)),
        ('minimum_normal', 2**-14),
        ('maximum_subnormal', 2**-14 - 2**-24),
        ('minimum_subnormal', 2**-24),
        ('half_minimum_subnormal', 2**-25),
        ('above_half_minimum_subnormal', math.nextafter(2**-25, math.inf)),
        ('positive_zero', 0.0),
        ('negative_zero', -0.0),
        ('positive_infinity', math.inf),
        ('negative_infinity', -math.inf),
        ('nan', math.nan),
    )
    for prefix in PREFIXES:
        for name, value in half_values:
            format_value = f'{prefix}e'
            prefix_name = {
                '': 'default',
                '@': 'native',
                '=': 'native_standard',
                '<': 'little',
                '>': 'big',
                '!': 'network',
            }[prefix]
            input_name = f'_half_{prefix_name}_{name}'
            packed = f'struct.pack({format_value!r}, {input_name})'
            _add_case(
                records,
                f'half:{prefix_name}:{name}',
                f'({packed}, struct.unpack({format_value!r}, {packed}))',
                format_value=format_value,
                float64_inputs_be={input_name: struct.pack('>d', value).hex()},
            )

    for format_value, values in (
        ('4s', (b'abcdef',)),
        ('4p', (b'abcdef',)),
        ('0s', (b'abc',)),
        ('0p', (b'abc',)),
        ('256p', (b'a' * 300,)),
        ('0i', ()),
        ('0x', ()),
    ):
        _add_case(
            records,
            f'count:{format_value}',
            _pack_round_trip(format_value, values),
            format_value=format_value,
        )

    for prefix in PREFIXES:
        for code, value in (
            ('f', 1e300),
            ('F', 1e300 + 0j),
            ('F', 0 + 1e300j),
            ('D', 1e300 + 0j),
            ('D', 0 + 1e300j),
        ):
            format_value = f'{prefix}{code}'
            _add_case(
                records,
                f'overflow:{format_value!r}',
                _pack_round_trip(format_value, (value,)),
                format_value=format_value,
            )

    pointer_bits = struct.calcsize('@P') * 8
    range_cases = (
        ('B', 256),
        ('b', -129),
        ('H', 1 << 16),
        ('h', -(1 << 15) - 1),
        ('I', 1 << 32),
        ('i', -(1 << 31) - 1),
        ('n', sys.maxsize + 1),
        ('N', -1),
        ('P', -1),
        ('P', 1 << pointer_bits),
    )
    for code, value in range_cases:
        format_value = f'@{code}' if code in 'nNP' else f'>{code}'
        _add_case(
            records,
            f'range:{format_value}:{value}',
            _pack_round_trip(format_value, (value,)),
            format_value=format_value,
        )

    _add_case(records, 'bytes-format:calcsize', "struct.calcsize(b'>I')", format_value=b'>I')
    _add_case(
        records,
        'bytes-format:pack-unpack',
        _pack_round_trip(b'>I', (640,)),
        format_value=b'>I',
    )

    for name, format_value in (
        ('empty', ''),
        ('invalid-code', 'Z'),
        ('repeated-prefix', '@@i'),
        ('conflicting-prefix', '<@I'),
        ('trailing-count', '2'),
        ('zero-count', '0i'),
    ):
        _add_case(
            records,
            f'malformed:{name}',
            f'struct.calcsize({format_value!r})',
            format_value=format_value,
        )

    _add_case(
        records,
        'large-count:calcsize',
        "struct.calcsize('1000000000i')",
        format_value='1000000000i',
    )
    _add_case(
        records,
        'large-count:unpack-empty',
        "struct.unpack('1000000000i', b'')",
        format_value='1000000000i',
    )
    _add_case(records, 'pack:missing-value', "struct.pack('I')", format_value='I')
    _add_case(records, 'pack:extra-value', "struct.pack('I', 1, 2)", format_value='I')
    _add_case(records, 'unpack:short-buffer', "struct.unpack('>I', b'')", format_value='>I')
    _add_case(
        records,
        'unpack:long-buffer',
        "struct.unpack('>I', b'\\x00\\x00\\x00\\x01x')",
        format_value='>I',
    )

    pointer_max = (1 << pointer_bits) - 1
    native_ranges = (
        ('n:minimum', 'n', -sys.maxsize - 1),
        ('n:maximum', 'n', sys.maxsize),
        ('n:below-minimum', 'n', -sys.maxsize - 2),
        ('n:above-maximum', 'n', sys.maxsize + 1),
        ('N:minimum', 'N', 0),
        ('N:maximum', 'N', pointer_max),
        ('N:above-maximum', 'N', pointer_max + 1),
        ('P:minimum', 'P', 0),
        ('P:maximum', 'P', pointer_max),
        ('P:above-maximum', 'P', pointer_max + 1),
    )
    for name, code, value in native_ranges:
        format_value = f'@{code}'
        _add_case(
            records,
            f'native-boundary:{name}',
            _pack_round_trip(format_value, (value,)),
            format_value=format_value,
        )

    raw_nan_payloads = (
        ('quiet_one', '7ff8000000000001'),
        ('quiet_payload', '7ff8000000000042'),
        ('negative_quiet', 'fff8000000000001'),
        ('signaling_one', '7ff0000000000001'),
    )
    for prefix in PREFIXES:
        format_value = f'{prefix}e'
        for name, payload in raw_nan_payloads:
            input_name = f'_raw_nan_{name}'
            packed = f'struct.pack({format_value!r}, {input_name})'
            input_echo = f"struct.pack('>d', {input_name})"
            _add_case(
                records,
                f'half-raw-nan:{prefix or "default"}:{name}',
                f'({input_echo}, {packed}, struct.unpack({format_value!r}, {packed}))',
                format_value=format_value,
                float64_inputs_be={input_name: payload},
            )

    traces = (
        (
            'index',
            "events = []\n"
            "class IndexValue:\n"
            "    def __index__(self):\n"
            "        events.append('__index__')\n"
            "        return 640\n"
            "value = IndexValue()",
            'struct.pack(\'I\', value)',
        ),
        (
            'float',
            "events = []\n"
            "class FloatValue:\n"
            "    def __float__(self):\n"
            "        events.append('__float__')\n"
            "        return 1.25\n"
            "value = FloatValue()",
            'struct.pack(\'d\', value)',
        ),
        (
            'complex',
            "events = []\n"
            "class ComplexValue:\n"
            "    def __complex__(self):\n"
            "        events.append('__complex__')\n"
            "        return 1 + 2j\n"
            "value = ComplexValue()",
            'struct.pack(\'D\', value)',
        ),
        (
            'format-before-index',
            "events = []\n"
            "class IndexValue:\n"
            "    def __index__(self):\n"
            "        events.append('__index__')\n"
            "        return 640\n"
            "value = IndexValue()",
            "struct.pack('Z', value)",
        ),
        (
            'arity-before-index',
            "events = []\n"
            "class IndexValue:\n"
            "    def __index__(self):\n"
            "        events.append('__index__')\n"
            "        return 640\n"
            "value = IndexValue()",
            "struct.pack('I', value, 1)",
        ),
    )
    for name, setup, expression in traces:
        _add_case(
            records,
            f'dunder:{name}',
            expression,
            setup=setup,
            trace_name='events',
            format_value=None,
        )

    return records


def main() -> int:
    environment = _environment()
    qualifies = environment['implementation'] == 'CPython' and environment['version_info'][:2] == [3, 14]
    payload: dict[str, object] = {
        'environment': environment,
        'qualifies': qualifies,
        'records': _records() if qualifies else [],
    }
    if not qualifies:
        payload['qualification_error'] = 'reference requires CPython 3.14'
    print(json.dumps(payload, allow_nan=False, separators=(',', ':'), sort_keys=True))
    return 0 if qualifies else 2


if __name__ == '__main__':
    raise SystemExit(main())
