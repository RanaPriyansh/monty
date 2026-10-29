from __future__ import annotations

import struct
import sys
from typing import Any

import pytest
from conftest import RunMonty

from pydantic_monty import Monty, MontyRuntimeError, MontyTypingError


def test_supported_api_with_type_checking(monty_run: RunMonty):
    assert monty_run("import struct\n(struct.calcsize('>I'), struct.unpack('>I', struct.pack('>I', 640)))") == (
        4,
        (640,),
    )


@pytest.mark.parametrize('name', ['Struct', 'pack_into', 'unpack_from', 'iter_unpack', '_clearcache'])
def test_excluded_api_fails_type_checking(monty_run: RunMonty, name: str):
    with pytest.raises(MontyTypingError):
        monty_run(f'import struct\nstruct.{name}')


@pytest.mark.parametrize(
    'expression',
    [
        'struct.pack()',
        "struct.pack(format='I')",
        'struct.calcsize()',
        "struct.calcsize('I', 'I')",
        "struct.calcsize(format='I')",
        'struct.unpack()',
        "struct.unpack('I')",
        "struct.unpack('I', b'', b'')",
        "struct.unpack(format='I', buffer=b'')",
        'struct.calcsize([])',
        'struct.calcsize({})',
        'struct.calcsize(1)',
        "struct.calcsize('é')",
        r"struct.calcsize(b'\xff')",
        "struct.unpack('Z', None)",
        "struct.unpack('I', None)",
        "struct.pack('c', b'')",
        "struct.pack('s', 'text')",
        "struct.pack('p', None)",
        "struct.pack('f', '1')",
        "struct.pack('e', 1e300)",
        "struct.pack('Q', 2**64)",
        "struct.pack('q', -2**63 - 1)",
    ],
)
def test_signature_and_error_order_matches_cpython(monty_run: RunMonty, expression: str):
    try:
        eval(expression, {'struct': struct})
    except Exception as error:
        expected = (type(error).__name__, str(error))
    else:
        pytest.fail(f'CPython did not reject {expression}')
    code = f'import struct\ntry:\n    {expression}\nexcept Exception as error:\n    result = (type(error).__name__, str(error))\nresult'
    assert monty_run(code, skip_type_check=True) == expected


@pytest.mark.parametrize(
    'method,code,result', [('__index__', 'Q', '[]'), ('__float__', 'd', '1'), ('__complex__', 'D', '1')]
)
def test_wrong_conversion_result_matches_cpython(monty_run: RunMonty, method: str, code: str, result: str):
    if method == '__complex__' and sys.version_info < (3, 14):
        pytest.skip('complex formats require CPython 3.14')
    program = f"""
import struct
trace = []
class First:
    def {method}(self):
        trace.append('first')
        return {result}
class Later:
    def {method}(self):
        trace.append('later')
        return {result}
try:
    struct.pack('{code}{code}', First(), Later())
except Exception as error:
    result = (type(error).__name__, str(error), trace)
"""
    namespace: dict[str, Any] = {}
    exec(program, namespace)
    assert monty_run(program + '\nresult', skip_type_check=True) == namespace['result']


def test_struct_error_crosses_the_worker_boundary(monty_run: RunMonty):
    with pytest.raises(MontyRuntimeError) as error:
        monty_run("import struct\nstruct.pack('Z')")
    assert type(error.value.exception()) is struct.error
    assert str(error.value.exception()) == 'bad char in struct format'
    assert monty_run('import struct\nstruct.error') is struct.error


def test_host_struct_error_enters_the_sandbox(monty_run: RunMonty):
    def fail() -> None:
        raise struct.error('host struct error')

    assert (
        monty_run(
            'import struct\ntry:\n    fail()\nexcept struct.error as error:\n    result = str(error)\nresult',
            external_lookup={'fail': fail},
            skip_type_check=True,
        )
        == 'host struct error'
    )


def test_pack_allocation_is_terminal_and_worker_recovers(pool: Monty):
    with pool.checkout(limits={'max_memory': 2_000_000}) as session:
        with pytest.raises(MontyRuntimeError) as error:
            session.feed_run(
                "import struct\ntry:\n    struct.pack('100000000x')\nexcept Exception:\n    result = 'caught'",
                skip_type_check=True,
            )
        assert isinstance(error.value.exception(), MemoryError)
    with pool.checkout() as session:
        assert session.feed_run('17') == 17


def test_unpacked_output_limit_is_terminal_and_worker_recovers(pool: Monty):
    with pool.checkout(limits={'max_memory': 2_000_000}) as session:
        assert session.feed_run("payload = b'x' * 100000\nlen(payload)", skip_type_check=True) == 100000
        with pytest.raises(MontyRuntimeError) as error:
            session.feed_run(
                "import struct\ntry:\n    struct.unpack('100000c', payload)\nexcept Exception:\n    result = 'caught'",
                skip_type_check=True,
            )
        assert isinstance(error.value.exception(), MemoryError)
    with pool.checkout() as session:
        assert session.feed_run('23') == 23


def test_conversion_resource_error_remains_terminal(pool: Monty):
    program = """
import struct
class Allocate:
    def __float__(self):
        return float(len('x' * 100000000))
try:
    struct.pack('d', Allocate())
except Exception:
    result = 'caught'
"""
    with pool.checkout(limits={'max_memory': 2_000_000}) as session:
        with pytest.raises(MontyRuntimeError) as error:
            session.feed_run(program, skip_type_check=True)
        assert isinstance(error.value.exception(), MemoryError)
    with pool.checkout() as session:
        assert session.feed_run('19') == 19


@pytest.mark.parametrize(
    'methods',
    [
        "def __bool__(self):\n        trace.append('bool')\n        return False\n    def __len__(self):\n        trace.append('len')\n        return 3",
        "def __len__(self):\n        trace.append('len')\n        return 0",
        'def __len__(self):\n        return -1',
        'def __len__(self):\n        return -2**100',
        'def __len__(self):\n        return -2**200',
        'def __len__(self):\n        return 2**100',
        'def __len__(self):\n        return 1.5',
        'def __bool__(self):\n        return 1',
        "def __bool__(self):\n        raise ValueError('truth failed')\n    def __len__(self):\n        trace.append('len')\n        return 0",
        'def __len__(self):\n        return Length()',
    ],
)
def test_truth_conversion_matches_cpython(monty_run: RunMonty, methods: str):
    program = f"""
import struct
trace = []
class Length:
    def __index__(self):
        trace.append('index')
        return 2
class Truth:
    {methods}
try:
    result = ('value', struct.pack('?', Truth()), trace)
except Exception as error:
    result = ('error', type(error).__name__, str(error), trace)
"""
    namespace: dict[str, Any] = {}
    exec(program, namespace)
    assert monty_run(program + '\nresult', skip_type_check=True) == namespace['result']


@pytest.mark.parametrize('length', ['-2**100', '-2**200'])
def test_index_returned_negative_length_matches_cpython(monty_run: RunMonty, length: str):
    program = f"""
import struct
trace = []
class Length:
    def __index__(self):
        trace.append('index')
        return {length}
class Truth:
    def __len__(self):
        trace.append('len')
        return Length()
try:
    struct.pack('?', Truth())
except Exception as error:
    result = (type(error).__name__, str(error), trace)
"""
    namespace: dict[str, Any] = {}
    exec(program, namespace)
    assert monty_run(program + '\nresult', skip_type_check=True) == namespace['result']


@pytest.mark.parametrize(
    'expression',
    [
        r"struct.calcsize(b'\xff')",
        r"struct.calcsize(b'\xc3\xa9')",
        r"struct.calcsize(b'\xffA\x80')",
        r"struct.calcsize(b'Z\x00\xff')",
        "struct.calcsize('Zé')",
        "struct.calcsize('Aé😀')",
        r"struct.pack(b'\xff')",
        r"struct.unpack(b'\xff', None)",
        r"struct.unpack(b'\xff')",
        r"struct.calcsize(b'Z\x00')",
        "struct.calcsize(b'Z')",
    ],
)
def test_unicode_format_order_matches_cpython(monty_run: RunMonty, expression: str):
    try:
        eval(expression, {'struct': struct})
    except Exception as error:
        expected = (type(error).__name__, str(error))
    else:
        pytest.fail(f'CPython did not reject {expression}')
    program = f'import struct\ntry:\n    {expression}\nexcept Exception as error:\n    result = (type(error).__name__, str(error))\nresult'
    assert monty_run(program, skip_type_check=True) == expected


def test_non_ascii_bytes_format_uses_documented_host_fallback(monty_run: RunMonty):
    with pytest.raises(UnicodeEncodeError) as expected:
        struct.calcsize(b'\xff')
    with pytest.raises(MontyRuntimeError) as error:
        monty_run(r"import struct; struct.calcsize(b'\xff')")
    converted = error.value.exception()
    assert type(converted) is ValueError
    assert str(converted) == str(expected.value)


def test_non_ascii_string_format_preserves_host_fields(monty_run: RunMonty):
    with pytest.raises(MontyRuntimeError) as error:
        monty_run("import struct\nstruct.calcsize('Aé😀')")
    converted = error.value.exception()
    assert isinstance(converted, UnicodeEncodeError)
    assert converted.encoding == 'ascii'
    assert converted.object == 'Aé😀'
    assert converted.start == 1
    assert converted.end == 3
    assert converted.reason == 'ordinal not in range(128)'


@pytest.mark.parametrize(
    'code', ['b', 'B', 'h', 'H', 'i', 'I', 'l', 'L', 'q', 'Q', 'n', 'N', 'P', '<q', '>q', '<Q', '>Q']
)
@pytest.mark.parametrize('body', ['return 2**128', 'return -2**128', "raise OverflowError('index overflow')"])
def test_integer_conversion_overflow_matches_cpython(monty_run: RunMonty, code: str, body: str):
    program = f"""
import struct
trace = []
class Index:
    def __index__(self):
        trace.append('index')
        {body}
try:
    struct.pack('{code}', Index())
except Exception as error:
    result = (type(error).__name__, str(error), trace)
"""
    namespace: dict[str, Any] = {}
    exec(program, namespace)
    assert monty_run(program + '\nresult', skip_type_check=True) == namespace['result']
