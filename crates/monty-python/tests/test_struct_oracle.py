from __future__ import annotations

import json
import math
import struct
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, NoReturn

import pytest
from conftest import RunMonty
from inline_snapshot import snapshot


def _run_cpython_oracle() -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[3]
    try:
        completed = subprocess.run(
            ['uv', 'run', '--no-sync', 'playground/struct_524_oracle.py'],
            cwd=repo_root,
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f'CPython struct oracle exceeded 60 seconds: {error}', pytrace=False)

    if completed.returncode != 0:
        pytest.fail(
            f'CPython struct oracle failed.\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}',
            pytrace=False,
        )

    return json.loads(completed.stdout)


def _normalize(value: Any) -> Any:
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


def _worker_program(records: list[dict[str, Any]]) -> str:
    lines = ['import struct', '_results = []']
    for record in records:
        trace_name = record.get('trace_name')
        lines.append('try:')
        if record['setup']:
            lines.extend(textwrap.indent(record['setup'], '    ').splitlines())
        lines.append(f'    _value = {record["expression"]}')
        if trace_name:
            lines.append(f"    _item = ('value', _value, {trace_name})")
        else:
            lines.append("    _item = ('value', _value)")
        lines.append('except Exception as _error:')
        if trace_name:
            lines.append(f"    _item = ('error', type(_error).__name__, str(_error), {trace_name})")
        else:
            lines.append("    _item = ('error', type(_error).__name__, str(_error))")
        lines.append('_results.append(_item)')
    lines.append('_results')
    return '\n'.join(lines)


def _worker_inputs(records: list[dict[str, Any]], oracle: dict[str, Any]) -> dict[str, float]:
    inputs: dict[str, float] = {}
    for record in records:
        for name, payload in record.get('float64_inputs_be', {}).items():
            value = struct.unpack('>d', bytes.fromhex(payload))[0]
            observed = struct.pack('>d', value).hex()
            expected = record['float64_inputs_after_decode_be'][name]
            if observed != expected:
                _oracle_failure(f'CPython input float changed its recorded NaN payload for {name}', oracle)
            inputs[name] = value
    return inputs


def _normalize_worker_record(record: tuple[Any, ...]) -> dict[str, Any]:
    if record[0] == 'value':
        result = {'kind': 'value', 'value': _normalize(record[1])}
        if len(record) == 3:
            result['trace'] = _normalize(record[2])
        return result

    result = {'kind': 'error', 'type': record[1], 'message': record[2]}
    if len(record) == 4:
        result['trace'] = _normalize(record[3])
    return result


def _oracle_failure(message: str, oracle: dict[str, Any], actual: Any = None) -> NoReturn:
    pytest.fail(
        f'{message}\n'
        f'CPython struct oracle records:\n{json.dumps(oracle, indent=2, sort_keys=True)}\n'
        f'Monty result:\n{json.dumps(actual, indent=2, sort_keys=True, default=repr)}',
        pytrace=False,
    )


def test_struct_runtime_smoke(monty_run: RunMonty):
    code = """
import struct
payload = b'\\x00\\x00\\x02\\x80\\x00\\x00\\x01\\xe0'
(struct.calcsize('>II'), struct.unpack('>II', payload), struct.pack('>II', 640, 480))
"""
    assert monty_run(code, skip_type_check=True) == snapshot((8, (640, 480), b'\x00\x00\x02\x80\x00\x00\x01\xe0'))


@pytest.mark.skipif(sys.version_info[:2] != (3, 14), reason='struct F and D require CPython 3.14')
def test_struct_matches_cpython_oracle(monty_run: RunMonty):
    oracle = _run_cpython_oracle()
    environment = oracle['environment']
    if environment['implementation'] != 'CPython' or environment['version_info'][:2] != [3, 14]:
        _oracle_failure('oracle did not run on CPython 3.14', oracle)

    records = oracle['records']
    try:
        result = monty_run(
            _worker_program(records),
            inputs=_worker_inputs(records, oracle),
            skip_type_check=True,
        )
    except Exception as error:
        _oracle_failure(
            f'Monty failed while running the CPython parity records: {type(error).__name__}: {error}',
            oracle,
        )

    actual = [_normalize_worker_record(record) for record in result]
    expected = [record['expected'] for record in records]
    if len(actual) != len(expected):
        _oracle_failure(
            f'expected {len(expected)} oracle results, received {len(actual)}',
            oracle,
            actual,
        )
    if actual != expected:
        mismatch = next(
            index
            for index, (actual_record, expected_record) in enumerate(zip(actual, expected, strict=False))
            if actual_record != expected_record
        )
        _oracle_failure(
            f'struct oracle mismatch at {records[mismatch]["name"]}',
            oracle,
            actual,
        )
