#!/usr/bin/env python3
"""Compare Monty #1037 compiler selection with identical packed Node smoke tests."""

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import traceback
from pathlib import Path

SOURCE = '9000dc6cfaffa34a652c5d8e33a55b7cee58c251'
TARGET = 'x86_64-unknown-linux-gnu'
IMAGE = 'node:22.13.1-bullseye@sha256:2a9beeeadb2681aff0b8716c422ddedb20b4d4f69f4d706c7cb535f8c4ba1659'
ADDON = 'monty.linux-x64-gnu.node'
ROOT = Path(__file__).resolve().parents[2]
CASE = ROOT / 'glibc-experiment'
EVIDENCE = CASE / 'evidence'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def check_disk(stage):
    free = shutil.disk_usage(CASE).free
    with (EVIDENCE / 'disk-space.jsonl').open('a') as log:
        log.write(json.dumps({'stage': stage, 'free_bytes': free}) + '\n')
    require(free >= 4 * 1024**3, f'Less than 4 GiB free before {stage}')


def run(args, cwd, log, env=None, expected=(0,)):
    with log.open('w') as output:
        output.write(f'cwd: {cwd}\ncommand: {shlex.join(map(str, args))}\n')
        output.flush()
        result = subprocess.run(args, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, check=False)
        output.write(f'\nexit: {result.returncode}\n')
    print(f'{log.relative_to(EVIDENCE)}: exit {result.returncode}', flush=True)
    if result.returncode not in expected:
        print('\n'.join(log.read_text(errors='replace').splitlines()[-80:]), flush=True)
    require(result.returncode in expected, f'Unexpected exit {result.returncode}: {log}')
    return result.returncode


def output(args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def dependency_versions(tree):
    return {
        name: {'version': item['version'], 'dependencies': dependency_versions(item)}
        for name, item in tree.get('dependencies', {}).items()
    }


def compiler_probe(args):
    directory = Path(os.environ['MONTY_TRACE_DIR'])
    relevant = {
        key: value
        for key, value in os.environ.items()
        if re.match(
            r'^(CC|CXX|HOST_|TARGET_|CARGO_TARGET_|AWS_LC_|CFLAGS|CXXFLAGS|CPATH|C_INCLUDE_PATH|CPLUS_INCLUDE_PATH|LIBRARY_PATH|PATH)',
            key,
        )
    }
    linker = Path(os.environ['CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER'])
    cc = shutil.which(f'{TARGET}-gcc')
    cxx = shutil.which(f'{TARGET}-g++')
    require(cc is not None and cxx is not None, 'napi child PATH lacks the cross compilers')
    require(Path(cc).resolve() == linker.resolve(), 'Compiler and linker do not match')
    tools = {}
    for name, compiler in (('cc', cc), ('cxx', cxx)):
        tools[name] = {
            'path': compiler,
            'sha256': digest(Path(compiler)),
            'target': output([compiler, '-dumpmachine']),
            'sysroot': output([compiler, '-print-sysroot']),
            'version': output([compiler, '--version']),
            'search': subprocess.run(
                [compiler, '-E', '-v', '-x', 'c++' if name == 'cxx' else 'c', '-'],
                input='',
                text=True,
                capture_output=True,
                check=True,
            ).stderr,
        }
        require(tools[name]['target'] == TARGET, 'Wrong compiler target')
        require(Path(tools[name]['sysroot']).is_dir(), 'Compiler sysroot is absent')
    require(tools['cc']['sysroot'] == tools['cxx']['sysroot'], 'C and C++ sysroots differ')
    require(
        Path(tools['cc']['sysroot']).resolve() == Path(os.environ['TARGET_SYSROOT']).resolve(),
        'napi and compiler sysroots differ',
    )
    require(Path(os.environ['TARGET_CC']).resolve() == linker.resolve(), 'napi C compiler differs from linker')
    require(Path(os.environ['TARGET_CXX']).resolve() == Path(cxx).resolve(), 'napi C++ compiler differs')
    save_json(directory / 'cargo-child.json', {'environment': relevant, 'tools': tools, 'cargo_args': args})
    print(json.dumps({'cargo_child': relevant, 'tools': tools}), flush=True)
    os.execv(os.environ['MONTY_REAL_CARGO'], [os.environ['MONTY_REAL_CARGO'], *args])


def symbols(binary, directory):
    text = output(['readelf', '--dyn-syms', '-W', str(binary)])
    (directory / 'dynamic-symbols.txt').write_text(text + '\n')
    undefined = '\n'.join(line for line in text.splitlines() if re.search(r'\bUND\b', line))
    (directory / 'undefined-symbols.txt').write_text(undefined + '\n')
    versions = sorted({tuple(map(int, version.split('.'))) for version in re.findall(r'GLIBC_([0-9.]+)', text)})
    floor = not versions or versions[-1] <= (2, 17)
    result = {
        'sha256': digest(binary),
        'maximum_versioned_glibc': versions[-1] if versions else None,
        'floor_pass': floor,
        'isoc23_undefined': re.findall(r'\S*__isoc23_\S*', undefined),
    }
    save_json(directory / 'symbols.json', result)
    return result


def pack(directory, destination, log, required):
    run(['npm', 'pack', '--ignore-scripts', '--json', '--pack-destination', str(destination)], directory, log)
    text = log.read_text()
    result = json.loads(text[text.index('[\n') : text.rindex(']') + 1])[0]
    require(set(required) <= {item['path'] for item in result['files']}, 'Packed package lacks required files')
    archive = destination / result['filename']
    with tarfile.open(archive) as tar:
        require(set('package/' + name for name in required) <= set(tar.getnames()), 'Tar file list differs')
        for name in (ADDON, 'monty'):
            if name in required:
                require(
                    hashlib.sha256(tar.extractfile('package/' + name).read()).hexdigest() == digest(directory / name),
                    'Packed artifact hash differs',
                )
    return archive


def main():
    require(not CASE.exists(), 'Experiment directory must be absent')
    EVIDENCE.mkdir(parents=True)
    check_disk('initial')
    require(os.environ.get('CARGO_BUILD_JOBS') == '2', 'Cargo must use two build jobs')
    require(output(['uname', '-m']) == 'x86_64', 'This experiment requires Linux x64')
    require(sys.platform == 'linux', 'This experiment requires Linux')
    require('MONTY_BIN' not in os.environ, 'MONTY_BIN must be absent')
    forbidden = {
        key: value
        for key, value in os.environ.items()
        if re.match(
            r'^(CC($|_)|CXX($|_)|HOST_CC|HOST_CXX|TARGET_CC|TARGET_CXX|CFLAGS|CXXFLAGS|CPATH|C_INCLUDE_PATH|CPLUS_INCLUDE_PATH|LIBRARY_PATH|AWS_LC_|CARGO_TARGET_.*(LINKER|RUSTFLAGS))',
            key,
        )
    }
    save_json(EVIDENCE / 'compiler-overrides.json', forbidden)
    require(not forbidden, 'Runner has conflicting compiler overrides')
    rust = output(['rustc', '-vV'])
    require(f'host: {TARGET}' in rust, 'This is not a native GNU Rust build')
    versions = {
        'source': SOURCE,
        'experiment_head': output(['git', 'rev-parse', 'HEAD'], ROOT),
        'runner_image': {key: os.environ.get(key) for key in ('ImageOS', 'ImageVersion', 'RUNNER_OS', 'RUNNER_ARCH')},
        'rust': rust,
        'cargo': output(['cargo', '--version']),
        'node': output(['node', '--version']),
        'npm': output(['npm', '--version']),
        'glibc': output(['getconf', 'GNU_LIBC_VERSION']),
    }
    save_json(EVIDENCE / 'versions.json', versions)
    real_cargo = shutil.which('cargo')
    wrapper = CASE / 'cargo-probe.sh'
    wrapper.write_text('#!/bin/sh\nexec python3 "$MONTY_EXPERIMENT_HELPER" cargo-probe "$@"\n')
    wrapper.chmod(0o755)
    records = {}
    for variant in ('baseline', 'candidate'):
        directory = EVIDENCE / variant
        directory.mkdir()
        source = CASE / variant
        run(['git', 'clone', '--shared', '--no-checkout', str(ROOT), str(source)], ROOT, directory / 'clone.log')
        run(['git', 'checkout', '--detach', SOURCE], source, directory / 'checkout.log')
        require(output(['git', 'rev-parse', 'HEAD'], source) == SOURCE, 'Wrong source')
        package = source / 'crates/monty-js'
        locks = {name: digest(source / name) for name in ('Cargo.lock', 'crates/monty-js/package-lock.json')}
        save_json(directory / 'locks-before.json', locks)
        env = os.environ.copy()
        env['npm_config_cache'] = str(CASE / 'npm-cache')
        run(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], package, directory / 'npm-ci.log', env)
        run(
            [
                'npm',
                'ls',
                '@napi-rs/cli',
                '@napi-rs/cross-toolchain',
                'typescript',
                '--json',
            ],
            package,
            directory / 'npm-tools.log',
            env,
        )
        tools = {
            name: json.loads((package / 'node_modules' / name / 'package.json').read_text())['version']
            for name in (
                '@napi-rs/cli',
                '@napi-rs/cross-toolchain',
                'typescript',
            )
        }
        require(tools['@napi-rs/cli'] == '3.7.1', 'Wrong napi CLI')
        save_json(directory / 'tools.json', tools)
        target = CASE / (variant + '-target')
        require(not target.exists(), 'Native target directory must be absent')
        env.update(
            {
                'CARGO_TARGET_DIR': str(target),
                'CARGO': str(wrapper),
                'MONTY_REAL_CARGO': real_cargo,
                'MONTY_EXPERIMENT_HELPER': str(Path(__file__).resolve()),
                'MONTY_TRACE_DIR': str(directory),
                'CC_ENABLE_DEBUG_OUTPUT': '1',
                'DEBUG': 'napi:*',
            }
        )
        if variant == 'candidate':
            env.update(
                {'CC_x86_64_unknown_linux_gnu': f'{TARGET}-gcc', 'CXX_x86_64_unknown_linux_gnu': f'{TARGET}-g++'}
            )
        check_disk(variant + '-addon')
        run(
            [
                str(package / 'node_modules/.bin/napi'),
                'build',
                '--platform',
                '--release',
                '--esm',
                '--target',
                TARGET,
                '--use-napi-cross',
                '--js',
                'native-addon.js',
                '--dts',
                'native-addon.generated.d.ts',
                '--',
                '--locked',
                '-vv',
            ],
            package,
            directory / 'addon-build.log',
            env,
        )
        addon = package / ADDON
        identity = symbols(addon, directory)
        build_trace = (directory / 'addon-build.log').read_text()
        require('aws-lc-sys' in build_trace, 'AWS-LC build trace is absent')
        if variant == 'candidate':
            require(f'{TARGET}-gcc' in build_trace and 'running:' in build_trace, 'C compiler commands are absent')
            require(
                not re.search(r'-(?:I|isystem|L)\s*[\"\x27]?/usr/(?:include|lib)(?:/|[\s\"\x27])', build_trace),
                'Candidate uses explicit host include or library paths',
            )
        shutil.copy2(addon, directory / ADDON)
        child = json.loads((directory / 'cargo-child.json').read_text())
        records[variant] = {
            'package': package,
            'source': source,
            'directory': directory,
            'env': env,
            'locks': locks,
            'tools': tools,
            'child': child,
            'symbols': identity,
        }
        require(locks == {name: digest(source / name) for name in locks}, 'Build changed a lock')
    baseline, candidate = records['baseline'], records['candidate']
    require(
        baseline['locks'] == candidate['locks'] and baseline['tools'] == candidate['tools'],
        'Variants have different dependencies',
    )
    require(baseline['child']['tools'] == candidate['child']['tools'], 'Variants have different cross toolchains')
    worker_directory = EVIDENCE / 'worker'
    worker_directory.mkdir()
    tools = candidate['child']['tools']
    worker_env = os.environ.copy()
    worker_env.update(
        {
            'CARGO_TARGET_DIR': str(CASE / 'worker-target'),
            'CC_x86_64_unknown_linux_gnu': tools['cc']['path'],
            'CXX_x86_64_unknown_linux_gnu': tools['cxx']['path'],
            'CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER': tools['cc']['path'],
            'CC_ENABLE_DEBUG_OUTPUT': '1',
        }
    )
    require(not Path(worker_env['CARGO_TARGET_DIR']).exists(), 'Worker target directory must be absent')
    save_json(
        worker_directory / 'compiler-environment.json',
        {key: value for key, value in worker_env.items() if key not in os.environ or os.environ[key] != value},
    )
    check_disk('shared-worker')
    run(
        [real_cargo, 'build', '--locked', '--release', '-p', 'monty-runtime', '--target', TARGET, '-vv'],
        candidate['source'],
        worker_directory / 'build.log',
        worker_env,
    )
    worker = Path(worker_env['CARGO_TARGET_DIR']) / TARGET / 'release/monty'
    require(worker.is_file(), 'Worker executable was not produced')
    worker_hash = digest(worker)
    shutil.copy2(worker, worker_directory / 'monty')
    symbols(worker, worker_directory)
    run(['docker', 'pull', '--platform', 'linux/amd64', IMAGE], ROOT, EVIDENCE / 'docker-pull.log')
    run(['docker', 'image', 'inspect', IMAGE], ROOT, EVIDENCE / 'docker-image.json')
    require(
        output(['docker', 'image', 'inspect', '--format', '{{.Os}}/{{.Architecture}}', IMAGE]) == 'linux/amd64',
        'Runtime image is not Linux AMD64',
    )
    results = {}
    for variant, record in records.items():
        package, directory, env = record['package'], record['directory'], record['env']
        run(
            [str(package / 'node_modules/.bin/napi'), 'create-npm-dirs'],
            package,
            directory / 'create-npm-dirs.log',
            env,
        )
        run(['node', 'scripts/create-platform-packages.mjs'], package, directory / 'platform-metadata.log', env)
        run(['npm', 'run', 'build:ts'], package, directory / 'build-ts.log', env)
        shutil.copytree(package / 'ts/worker/component', package / 'dist/worker/component', dirs_exist_ok=True)
        platform = package / 'npm/linux-x64-gnu'
        shutil.copy2(package / ADDON, platform / ADDON)
        shutil.copy2(worker, platform / 'monty')
        (platform / 'monty').chmod(0o755)
        archives = directory / 'tarballs'
        archives.mkdir()
        main_tar = pack(
            package,
            archives,
            directory / 'pack-main.log',
            ['dist/node.js', 'dist/node.d.ts', 'native-addon.js', 'native-addon.d.ts'],
        )
        platform_tar = pack(platform, archives, directory / 'pack-platform.log', ['package.json', ADDON, 'monty'])
        extracted = directory / 'extracted'
        extracted.mkdir()
        with tarfile.open(platform_tar) as archive:
            for name in (ADDON, 'monty'):
                (extracted / name).write_bytes(archive.extractfile('package/' + name).read())
        (extracted / 'monty').chmod(0o755)
        smoke = directory / 'smoke'
        smoke.mkdir()
        (smoke / 'package.json').write_text('{"private":true,"type":"module"}\n')
        smoke_hashes = {}
        for name in ('test.ts', 'node.ts', 'wasm-types.ts', 'tsconfig.json'):
            shutil.copy2(package / 'smoke-test' / name, smoke / name)
            smoke_hashes[name] = digest(smoke / name)
            require(smoke_hashes[name] == digest(package / 'smoke-test' / name), 'Smoke source differs')
        save_json(directory / 'smoke-source-hashes.json', smoke_hashes)
        run(
            [
                'npm',
                'install',
                str(main_tar),
                str(platform_tar),
                '--force',
                '--no-save',
                '--no-package-lock',
                '--ignore-scripts',
                '--omit=optional',
                '--no-audit',
                '--no-fund',
                *(['--offline'] if variant == 'candidate' else []),
            ],
            smoke,
            directory / 'install-packed.log',
            env,
        )
        installed_dependencies = json.loads(output(['npm', 'ls', '--all', '--json'], smoke))
        record['installed_dependencies'] = dependency_versions(installed_dependencies)
        save_json(directory / 'installed-dependencies.json', installed_dependencies)
        types = smoke / 'node_modules/@types'
        types.mkdir(exist_ok=True)
        (types / 'node').symlink_to(package / 'node_modules/@types/node', target_is_directory=True)
        run(
            [str(package / 'node_modules/.bin/tsc'), '-p', str(smoke / 'tsconfig.json')],
            smoke,
            directory / 'compile-smoke.log',
            env,
        )
        installed = smoke / 'node_modules/@pydantic/monty-linux-x64-gnu'
        require(digest(installed / ADDON) == record['symbols']['sha256'], 'Installed addon differs')
        require(digest(installed / 'monty') == worker_hash, 'Installed worker differs')
        artifact_hashes = {
            'addon': digest(extracted / ADDON),
            'worker': digest(extracted / 'monty'),
            'main_tar': digest(main_tar),
            'platform_tar': digest(platform_tar),
        }
        save_json(directory / 'artifact-hashes.json', artifact_hashes)
        runtime = directory / 'runtime.sh'
        runtime.write_text("""#!/bin/bash
set -eu
node --version
test "$(node --version)" = v22.13.1
test "$(uname -m)" = x86_64
getconf GNU_LIBC_VERSION
glibc=$(getconf GNU_LIBC_VERSION | cut -d ' ' -f2)
test "$(printf '%s\\n2.34\\n' "$glibc" | sort -V | tail -n1)" = 2.34
test -z "${MONTY_BIN+x}"
cd /case
printf '%s  %s\\n' "$EXPECTED_ADDON" extracted/monty.linux-x64-gnu.node "$EXPECTED_ADDON" smoke/node_modules/@pydantic/monty-linux-x64-gnu/monty.linux-x64-gnu.node "$EXPECTED_WORKER" extracted/monty "$EXPECTED_WORKER" smoke/node_modules/@pydantic/monty-linux-x64-gnu/monty | sha256sum -c -
set +e
node --require /case/extracted/monty.linux-x64-gnu.node </dev/null > load.log 2>&1
load=$?
./extracted/monty --help > worker.log 2>&1
worker=$?
node smoke/dist/node.js > smoke.log 2>&1
smoke=$?
set -e
printf '%s %s %s\\n' "$load" "$worker" "$smoke" > runtime-status.txt
cat load.log worker.log smoke.log
""")
        run(
            [
                'docker',
                'run',
                '--rm',
                '--platform',
                'linux/amd64',
                '--network',
                'none',
                '--env',
                f'EXPECTED_ADDON={record["symbols"]["sha256"]}',
                '--env',
                f'EXPECTED_WORKER={worker_hash}',
                '--mount',
                f'type=bind,source={directory},target=/case',
                IMAGE,
                'bash',
                '/case/runtime.sh',
            ],
            ROOT,
            directory / 'runtime.log',
        )
        statuses = list(map(int, (directory / 'runtime-status.txt').read_text().split()))
        results[variant] = {'load': statuses[0], 'worker': statuses[1], 'smoke': statuses[2]}
        require(
            record['locks'] == {name: digest(record['source'] / name) for name in record['locks']},
            'Packaging changed a lock',
        )
        require(
            not output(['git', 'diff', '--name-only', '--', '*.ts', '*.js', '*.mjs'], record['source']),
            'Tracked JavaScript or TypeScript changed',
        )
    save_json(EVIDENCE / 'runtime-results.json', results)
    require(
        baseline['installed_dependencies'] == candidate['installed_dependencies'], 'Packed smoke dependencies differ'
    )
    require(results['baseline']['load'] != 0, 'Baseline loaded: return for diagnosis')
    baseline_error = (baseline['directory'] / 'load.log').read_text()
    require(
        {'__isoc23_sscanf', '__isoc23_strtol'} & set(baseline['symbols']['isoc23_undefined']),
        'Baseline lacks the reported unversioned imports: return for diagnosis',
    )
    require(
        re.search(r'(?:undefined symbol|symbol not found).*__isoc23_(?:sscanf|strtol)', baseline_error),
        'Wrong baseline error: return for diagnosis',
    )
    require(
        results['baseline']['worker'] == 0 and results['candidate']['worker'] == 0,
        'Worker fails first: return for diagnosis',
    )
    require(results['baseline']['smoke'] != 0, 'Baseline packed smoke unexpectedly passed')
    require(
        results['candidate']['load'] == 0 and results['candidate']['smoke'] == 0,
        'Candidate load or packed smoke failed',
    )
    require(candidate['symbols']['floor_pass'], 'Candidate fails the existing GLIBC floor')
    require(not candidate['symbols']['isoc23_undefined'], 'Candidate retains undefined __isoc23 imports')
    (EVIDENCE / 'accepted.txt').write_text(
        'Baseline symbol failure and candidate packed Node smoke verified. Compiler traces require independent review.\n'
    )


if __name__ == '__main__':
    if sys.argv[1:2] == ['cargo-probe']:
        compiler_probe(sys.argv[2:])
    else:
        try:
            main()
        except Exception:
            if EVIDENCE.is_dir():
                (EVIDENCE / 'failure.txt').write_text(traceback.format_exc())
            raise
