import argparse
import hashlib
import json
from pathlib import Path
import platform
import struct
import sys
from time import perf_counter_ns


BASELINE_ERROR = (
    'monty worker protocol error: invalid payload from worker: failed to decode Protobuf message: '
    'MontyNode.kind: Complete.values: ChildEvent.kind: frame exceeds decode memory budget'
)
FINISHED_ERROR = 'this checkout has already been finished'
CANDIDATE_ERROR = 'RuntimeError: response exceeds host decode budget'
SIZES = (16, 1024, 65536)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('identity', 'baseline', 'candidate', 'neighbor', 'latency'))
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--worker', required=True, type=Path)
    parser.add_argument('--worker-sha256', required=True)
    parser.add_argument('--compiled-library', required=True, type=Path)
    parser.add_argument('--compiled-sha256', required=True)
    parser.add_argument('--count', type=int, default=16)
    return parser.parse_args()


def identities(args, package, native):
    require(__debug__ and sys.flags.optimize == 0, 'Python assertions must be enabled')
    require(sys.version_info[:2] == (3, 14), 'Python 3.14 is required')
    root = args.source_root.resolve(strict=True)
    package_root = root / 'crates/monty-python/python/pydantic_monty'
    package_path = Path(package.__file__).resolve(strict=True)
    native_path = Path(native.__file__).resolve(strict=True)
    compiled = args.compiled_library.resolve(strict=True)
    worker = args.worker.resolve(strict=True)
    require(package_path == package_root / '__init__.py', 'Python package source path differs')
    require(native_path.parent == package_root and native_path.name.startswith('_monty.'),
            'Loaded native library path differs')
    compiled_hash = file_hash(compiled)
    require(compiled_hash == args.compiled_sha256, 'Compiled native library hash differs')
    require(file_hash(native_path) == compiled_hash, 'Loaded native library hash differs from compiled output')
    require(file_hash(worker) == args.worker_sha256, 'Worker binary hash differs')
    return {
        'event': 'identity', 'python_version': sys.version, 'python_executable': str(Path(sys.executable).resolve()),
        'python_prefix': str(Path(sys.prefix).resolve()),
        'pointer_bytes': struct.calcsize('P'), 'platform': sys.platform, 'machine': platform.machine(),
        'assertions_enabled': True, 'source_root': str(root), 'package_source': str(package_path),
        'package_source_sha256': file_hash(package_path),
        'native_library': str(native_path), 'native_sha256': compiled_hash,
        'compiled_library': str(compiled), 'compiled_sha256': compiled_hash,
        'worker_binary': str(worker), 'worker_sha256': args.worker_sha256,
        'worker_identity_interface': 'MontySession.worker_pid',
    }


def check_list(value, count):
    require(type(value) is list and len(value) == count, 'List type or length differs')
    for index in (0, count // 2, count - 1):
        require(type(value[index]) is int and value[index] == index, 'List value differs')


def exercise(session, mode, count, runtime_error):
    require(type(count) is int and count > 0, 'Count must be a positive integer')
    initial = session.feed_run('sentinel = 17\n42')
    require(type(initial) is int and initial == 42, 'Initial sentinel differs')
    worker_before = session.worker_pid
    require(type(worker_before) is int and worker_before > 0, 'Initial worker PID is missing or invalid')
    if mode == 'neighbor':
        value = session.feed_run(f'list(range({count}))')
        check_list(value, count)
        del value
        result = {'event': 'result', 'mode': mode, 'count': count, 'values': 'checked'}
    elif mode == 'latency':
        samples = {}
        for size in SIZES:
            code = f'list(range({size}))'
            for _ in range(3):
                value = session.feed_run(code)
                check_list(value, size)
                del value
            samples[str(size)] = []
            for _ in range(10):
                started = perf_counter_ns()
                value = session.feed_run(code)
                elapsed = perf_counter_ns() - started
                check_list(value, size)
                del value
                samples[str(size)].append(elapsed)
        result = {'event': 'result', 'mode': mode, 'warmups_per_size': 3, 'samples_ns': samples,
                  'timed_calls_per_size': 10, 'checks_outside_timing': True}
    else:
        try:
            value = session.feed_run(f'list(range({count}))')
        except runtime_error as error:
            require(mode == 'candidate' and type(error) is runtime_error, 'Unexpected Monty error type')
            diagnostic = error.display(format='type-msg')
            require(diagnostic == CANDIDATE_ERROR, 'Candidate error message differs')
            worker_after_error = session.worker_pid
            require(type(worker_after_error) is int and worker_after_error == worker_before,
                    'Worker PID changed after candidate rejection')
            result = {'event': 'result', 'mode': mode, 'count': count,
                      'first_error_type': type(error).__name__, 'first_error': diagnostic,
                      'worker_after_error': worker_after_error}
        except RuntimeError as error:
            require(mode == 'baseline' and type(error) is RuntimeError, 'Unexpected built-in error type')
            require(str(error) == BASELINE_ERROR, 'Baseline error message differs')
            worker_after_error = session.worker_pid
            require(worker_after_error is None, 'Baseline worker PID was retained after Protocol rejection')
            try:
                session.feed_run('sentinel')
            except RuntimeError as after:
                require(type(after) is RuntimeError and str(after) == FINISHED_ERROR,
                        'Baseline session did not report the expected finished error')
                worker_after_sentinel = session.worker_pid
                require(worker_after_sentinel is None, 'Baseline worker PID returned after the finished sentinel')
                return {'event': 'result', 'mode': mode, 'count': count,
                        'first_error_type': 'RuntimeError', 'first_error': str(error),
                        'after_error_type': 'RuntimeError', 'after_error': str(after),
                        'worker_before': worker_before, 'worker_after_error': worker_after_error,
                        'worker_after_sentinel': worker_after_sentinel, 'worker_pid_checked': True}
            raise AssertionError('Baseline session unexpectedly completed the sentinel')
        else:
            del value
            raise AssertionError('The over-budget result unexpectedly completed')
    after = session.feed_run('sentinel')
    require(type(after) is int and after == 17, 'Same public session did not preserve sentinel 17')
    worker_after_sentinel = session.worker_pid
    require(type(worker_after_sentinel) is int and worker_after_sentinel == worker_before,
            'Worker PID changed after the final sentinel')
    result['after_error'] = 'Complete17'
    result['worker_before'] = worker_before
    result['worker_after_sentinel'] = worker_after_sentinel
    result['worker_pid_checked'] = True
    return result


def main():
    args = arguments()
    import pydantic_monty
    import pydantic_monty._monty as native

    identity = identities(args, pydantic_monty, native)
    print(json.dumps(identity, sort_keys=True), flush=True)
    if args.mode == 'identity':
        return
    with pydantic_monty.Monty(binary_path=args.worker, min_processes=0, max_processes=1) as pool:
        with pool.checkout() as session:
            result = exercise(session, args.mode, args.count, pydantic_monty.MontyRuntimeError)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
