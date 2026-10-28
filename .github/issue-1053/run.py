import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time


BASELINE = "0f243e6ae6df760f09040a17604e23d5264b65ba"
CANDIDATE = "62a622c55dc919a6e527715764050a93cd54c26c"
FIXTURE_SHA256 = "f5619080d0d6b003cbe5c2a057b250ea968b581d4d5b3fb281ff556925bd662e"
GIB = 1024**3
MEMORY_FLOOR = 6 * GIB
LARGE_MEMORY_FLOOR = 12 * GIB
RSS_LIMIT = 6 * GIB
DISK_FLOOR = 8 * GIB
TOTAL_SECONDS = 45 * 60
CLEANUP_SECONDS = 120
METADATA_KEYS = {
    "node_bytes", "usize_bytes", "default_max_decode_bytes", "max_frame_len",
    "first_over_budget_m", "charge_m_minus_1", "charge_m",
}
MACOS_REFERENCE_METADATA = {
    "node_bytes": 72, "usize_bytes": 8, "default_max_decode_bytes": GIB,
    "max_frame_len": 268435456, "first_over_budget_m": 8143827,
    "charge_m_minus_1": 1073741784, "charge_m": 1073741920,
}


def fields(output):
    result = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            if key in result:
                raise ValueError(f"duplicate output field: {key}")
            result[key] = value
    return result


def decode_charge(count, node_bytes, pointer_bytes):
    start = 0
    width = 1
    encoded_bytes = 0
    while start < count:
        end = min(count, 1 << (7 * width))
        encoded_bytes += (end - start) * width
        start = end
        width += 1
    return (count + 1) * node_bytes + 2 * pointer_bytes * encoded_bytes


def metadata(output):
    raw = fields(output)
    if set(raw) != METADATA_KEYS:
        raise ValueError("metadata fields differ from the reviewed fixture")
    values = {key: int(value) for key, value in raw.items()}
    if any(value <= 0 for value in values.values()):
        raise ValueError("metadata contains a nonpositive value")
    if values["default_max_decode_bytes"] != GIB:
        raise ValueError("the default decode budget is not 1 GiB")
    count = values["first_over_budget_m"]
    below = decode_charge(count - 1, values["node_bytes"], values["usize_bytes"])
    above = decode_charge(count, values["node_bytes"], values["usize_bytes"])
    if (below != values["charge_m_minus_1"] or above != values["charge_m"]
            or not below <= GIB < above):
        raise ValueError("metadata does not identify the canonical budget boundary")
    return values


def check_neighbor(output, count):
    values = fields(output)
    before = values.get("worker_before", "")
    if (not before.isdecimal() or values.get("neighbor_length") != str(count)
            or values.get("neighbor_values") != "checked"
            or values.get("worker_after_list") != f"Some({before})"
            or values.get("worker_after_sentinel") != f"Some({before})"):
        raise ValueError("neighbor output did not prove list values and worker retention")


def check_rejection(output, baseline):
    values = fields(output)
    before = values.get("worker_before", "")
    diagnostic = values.get("first_failure", "")
    if not before.isdecimal() or "decode" not in diagnostic or "budget" not in diagnostic:
        raise ValueError("rejection output lacks worker identity or decode-budget evidence")
    if baseline:
        if (values.get("first_failure_variant") != "Protocol"
                or values.get("worker_after_error") != "None"
                or values.get("worker_after_sentinel") != "None"
                or not values.get("after_error", "").startswith("Finished:")
                or values.get("cleanup_failure")
                != "checkout cleanup failed: this checkout has already been finished"):
            raise ValueError("baseline did not prove Protocol rejection and discarded checkout")
    elif (values.get("first_failure_variant") != "Runtime"
            or values.get("worker_after_error") != f"Some({before})"
            or values.get("worker_after_sentinel") != f"Some({before})"
            or values.get("after_error") != "Complete17"):
        raise ValueError("candidate did not prove Runtime rejection and same-worker recovery")


def proc_stat(text):
    process_fields = text[text.rindex(")") + 2:].split()
    return {
        "state": process_fields[0], "parent": int(process_fields[1]),
        "group": int(process_fields[2]), "start": int(process_fields[19]),
        "rss": max(0, int(process_fields[21])) * os.sysconf("SC_PAGE_SIZE"),
    }


def process_table():
    table = {}
    for path in Path("/proc").iterdir():
        if path.name.isdecimal():
            try:
                table[int(path.name)] = proc_stat((path / "stat").read_text())
            except (FileNotFoundError, ProcessLookupError):
                continue
    return table


def available_memory():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("Linux MemAvailable is unavailable")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Validation:
    def __init__(self):
        self.root = Path(__file__).resolve().parents[2]
        self.temporary = Path(os.environ["RUNNER_TEMP"]).resolve()
        self.evidence = self.temporary / "issue-1053-evidence"
        self.evidence.mkdir(parents=True, exist_ok=True)
        started = float(os.environ["ISSUE1053_STARTED"])
        elapsed = time.time() - started
        if not 0 <= elapsed < TOTAL_SECONDS - CLEANUP_SECONDS:
            raise RuntimeError("the absolute job time budget is unavailable or exhausted")
        self.deadline = time.monotonic() + TOTAL_SECONDS - elapsed
        self.work_deadline = self.deadline - CLEANUP_SECONDS
        self.owned = {}
        self.active = None
        self.interrupted = False
        self.report = {
            "status": "started", "commands": [], "checkouts": {}, "neighbors": {},
            "limits": {
                "total_seconds": TOTAL_SECONDS, "cleanup_seconds": CLEANUP_SECONDS,
                "memory_floor_bytes": MEMORY_FLOOR, "large_start_memory_bytes": LARGE_MEMORY_FLOOR,
                "sampled_owned_rss_limit_bytes": RSS_LIMIT, "hosted_disk_floor_bytes": DISK_FLOOR,
                "disk_scope": "Hosted temporary storage only. Local guards are unchanged.",
                "sampling_seconds": 0.5,
            },
            "comparison_limit": (
                "Compare other platforms only when layout, budgets, M and charges match. "
                "This run does not establish results for other binaries or platforms."
            ),
        }
        self.samples = (self.evidence / "resources.jsonl").open("a", buffering=1)
        self.save()

    def save(self):
        self.report["owned_process_start_ticks"] = self.owned
        (self.evidence / "result.json").write_text(json.dumps(self.report, indent=2) + "\n")

    def interrupted_by(self, number, _frame):
        if not self.interrupted:
            self.interrupted = True
            raise InterruptedError(f"received signal {number}")

    def discover(self):
        table = process_table()
        parents = {os.getpid()}
        parents.update(pid for pid, start in self.owned.items()
                       if pid in table and table[pid]["start"] == start)
        changed = True
        while changed:
            changed = False
            for pid, info in table.items():
                if pid != os.getpid() and info["parent"] in parents and pid not in parents:
                    self.owned[pid] = info["start"]
                    parents.add(pid)
                    changed = True
        return {pid: table[pid] for pid, start in self.owned.items()
                if pid in table and table[pid]["start"] == start}

    def guard(self, large=False):
        owned = self.discover()
        available = available_memory()
        disk = shutil.disk_usage(self.temporary).free
        descendant_rss = sum(info["rss"] for info in owned.values())
        supervisor_rss = proc_stat(Path("/proc/self/stat").read_text())["rss"]
        rss = descendant_rss + supervisor_rss
        sample = {"monotonic": time.monotonic(), "available_bytes": available,
                  "disk_free_bytes": disk, "owned_rss_bytes": rss,
                  "descendant_rss_bytes": descendant_rss, "supervisor_rss_bytes": supervisor_rss,
                  "owned_processes": owned}
        self.samples.write(json.dumps(sample) + "\n")
        if sample["monotonic"] >= self.work_deadline:
            raise RuntimeError("the absolute work deadline was reached")
        if available < (LARGE_MEMORY_FLOOR if large else MEMORY_FLOOR):
            raise RuntimeError("MemAvailable is below the required floor")
        if disk < DISK_FLOOR:
            raise RuntimeError("hosted temporary disk space is below the 8 GiB floor")
        if rss >= RSS_LIMIT:
            raise RuntimeError("sampled aggregate owned RSS reached 6 GiB")

    def cleanup(self):
        stop = min(self.deadline, time.monotonic() + CLEANUP_SECONDS)
        term_until = min(stop, time.monotonic() + 5)
        empty_samples = 0
        while True:
            owned = self.discover()
            if self.active is not None:
                self.active.poll()
            has_children = self.active is not None and self.active.returncode is None
            if self.active is None or self.active.returncode is not None:
                while True:
                    try:
                        child, _status = os.waitpid(-1, os.WNOHANG)
                    except ChildProcessError:
                        break
                    if child == 0:
                        has_children = True
                        break
                owned = self.discover()
            empty_samples = empty_samples + 1 if not owned and not has_children else 0
            if empty_samples >= 2:
                return []
            if time.monotonic() >= stop:
                if not owned:
                    raise RuntimeError("cleanup could not verify child absence before its deadline")
                return sorted(owned)
            number = signal.SIGTERM if time.monotonic() < term_until else signal.SIGKILL
            groups = {info["group"] for info in owned.values()} - {os.getpgrp()}
            for group in groups:
                try:
                    os.killpg(group, number)
                except ProcessLookupError:
                    pass
            for pid in owned:
                try:
                    os.kill(pid, number)
                except ProcessLookupError:
                    pass
            time.sleep(min(0.1, max(0, stop - time.monotonic())))

    def command(self, label, arguments, directory, timeout, expected=0, large=False, target=None):
        self.guard(large)
        record = {"label": label, "argv": [str(arg) for arg in arguments], "cwd": str(directory),
                  "timeout_seconds": timeout, "expected_exit": expected, "status": "starting"}
        self.report["commands"].append(record)
        self.save()
        overrides = {"CARGO_BUILD_JOBS": "2", "RAYON_NUM_THREADS": "1", "RUST_TEST_THREADS": "1",
                     "CARGO_INCREMENTAL": "0"}
        if target is not None:
            overrides["CARGO_TARGET_DIR"] = str(target)
        record["environment_overrides"] = overrides
        log = self.evidence / f"{len(self.report['commands']):02d}-{label}.log"
        record["log"] = log.name
        began = time.monotonic()
        failure = None
        try:
            with log.open("w") as output:
                self.active = subprocess.Popen(record["argv"], cwd=directory,
                                               env=os.environ | overrides, stdin=subprocess.DEVNULL,
                                               stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                record["pid"] = self.active.pid
                record["status"] = "running"
                self.save()
                while True:
                    self.guard()
                    if self.active.poll() is not None:
                        break
                    if time.monotonic() - began >= timeout:
                        raise TimeoutError(f"{label} exceeded {timeout} seconds")
                    time.sleep(0.5)
        except BaseException as error:
            failure = error
            record["failure"] = f"{type(error).__name__}: {error}"
        finally:
            try:
                survivors = self.cleanup()
            except BaseException as error:
                survivors = sorted(self.owned)
                record["cleanup_failure"] = f"{type(error).__name__}: {error}"
                if failure is None:
                    failure = error
            record["cleanup_survivors"] = survivors
            record["elapsed_seconds"] = time.monotonic() - began
            record["exit_code"] = self.active.poll() if self.active is not None else None
            record["status"] = "failed" if failure or survivors else "completed"
            self.active = None
            self.save()
        if failure is not None:
            raise failure
        if survivors:
            raise RuntimeError(f"cleanup could not verify process absence: {survivors}")
        if record["exit_code"] != expected:
            record["status"] = "unexpected_exit"
            self.save()
            raise RuntimeError(f"{label} exited {record['exit_code']}, expected {expected}")
        return log.read_text()

    def run(self):
        if sys.platform != "linux":
            raise RuntimeError("this validation requires Linux /proc and child subreaping")
        library = ctypes.CDLL(None, use_errno=True)
        enabled = ctypes.c_int()
        if (library.prctl(36, 1, 0, 0, 0) != 0
                or library.prctl(37, ctypes.byref(enabled), 0, 0, 0) != 0 or enabled.value != 1):
            raise RuntimeError("Linux child subreaping is unavailable")
        for number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(number, self.interrupted_by)
        self.guard()
        fixture = self.root / ".github/issue-1053/witness.rs"
        self.report["fixture_sha256"] = sha256(fixture)
        self.report["runner_sha256"] = sha256(Path(__file__))
        if self.report["fixture_sha256"] != FIXTURE_SHA256:
            raise RuntimeError("the reviewed fixture bytes changed")
        self.report["rust"] = self.command("rust-version", ["rustc", "-vV"], self.root, 30)
        self.report["cargo"] = self.command("cargo-version", ["cargo", "-Vv"], self.root, 30)
        self.report["workflow_source"] = self.command(
            "workflow-source", ["git", "rev-parse", "HEAD", "HEAD^{tree}"], self.root, 30
        ).splitlines()
        work = Path(tempfile.mkdtemp(prefix="issue-1053-", dir=self.temporary))
        self.report["work_directory"] = str(work)
        builds = {}
        for name, revision in (("baseline", BASELINE), ("candidate", CANDIDATE)):
            checkout = work / name
            target = work / f"target-{name}"
            self.command(f"{name}-clone", ["git", "clone", "--shared", "--no-checkout", self.root, checkout],
                         self.root, 30)
            self.command(f"{name}-checkout", ["git", "checkout", "--detach", revision], checkout, 30)
            source = self.command(f"{name}-source", ["git", "rev-parse", "HEAD", "HEAD^{tree}"], checkout, 30)
            commit, tree = source.splitlines()
            if commit != revision:
                raise RuntimeError(f"{name} source revision does not match")
            self.report["checkouts"][name] = {"commit": commit, "tree": tree, "target": str(target)}
            example = checkout / "crates/monty-pool/examples/issue1053_witness.rs"
            if example.exists():
                raise RuntimeError("the witness would overwrite a source file")
            example.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fixture, example)
            for label, selection in (("runtime", ["-p", "monty-runtime", "--bin", "monty"]),
                                     ("witness", ["-p", "monty-pool", "--example", "issue1053_witness"])):
                self.command(f"{name}-build-{label}",
                             ["cargo", "build", "--release", "--locked", "--jobs", "2", *selection],
                             checkout, 900, target=target)
            worker = target / "release/monty"
            witness = target / "release/examples/issue1053_witness"
            self.report["checkouts"][name].update({"worker_sha256": sha256(worker),
                                                   "witness_sha256": sha256(witness)})
            output = self.command(f"{name}-metadata", [witness, "metadata"], checkout, 30)
            self.report["checkouts"][name]["metadata"] = metadata(output)
            builds[name] = (checkout, worker, witness)
            self.save()
        baseline_metadata = self.report["checkouts"]["baseline"]["metadata"]
        if baseline_metadata != self.report["checkouts"]["candidate"]["metadata"]:
            raise RuntimeError("baseline and candidate metadata differ")
        self.report["macos_reference_comparison"] = {
            "reference_metadata": MACOS_REFERENCE_METADATA,
            "metadata_matches": baseline_metadata == MACOS_REFERENCE_METADATA,
            "limit": "Matching metadata permits numeric comparison only. It does not verify macOS recovery.",
        }
        count = baseline_metadata["first_over_budget_m"]
        for name, (checkout, worker, witness) in builds.items():
            output = self.command(f"{name}-healthy-16", [witness, "neighbor", worker, "16"], checkout, 120)
            check_neighbor(output, 16)
        for name, (checkout, worker, witness) in builds.items():
            output = self.command(f"{name}-reject-m", [witness, "reject", worker, str(count)], checkout, 360,
                                  expected=1 if name == "baseline" else 0, large=True)
            check_rejection(output, name == "baseline")
        self.report["default_budget_pair"] = "passed"
        self.save()
        for name, (checkout, worker, witness) in builds.items():
            if self.work_deadline - time.monotonic() < 425:
                self.report["neighbors"][name] = "omitted: insufficient time for the optional M-1 probe"
                continue
            self.guard()
            if available_memory() < LARGE_MEMORY_FLOOR:
                self.report["neighbors"][name] = "omitted before launch: MemAvailable is below 12 GiB"
                continue
            output = self.command(f"{name}-neighbor-m-minus-1",
                                  [witness, "neighbor", worker, str(count - 1)], checkout, 360, large=True)
            check_neighbor(output, count - 1)
            self.report["neighbors"][name] = "passed"
            self.save()
        for name, (checkout, _worker, _witness) in builds.items():
            output = self.command(f"{name}-tracked-status",
                                  ["git", "status", "--porcelain", "--untracked-files=no"], checkout, 30)
            if output.strip():
                raise RuntimeError(f"{name} tracked source changed")
            self.report["checkouts"][name]["tracked_source_unchanged"] = True
        self.report["status"] = "passed"


def main():
    validation = Validation()
    try:
        validation.run()
    except BaseException as error:
        validation.report["status"] = "failed"
        validation.report["failure"] = f"{type(error).__name__}: {error}"
    finally:
        try:
            survivors = validation.cleanup()
            validation.report["final_cleanup"] = {"verified_absent": not survivors, "survivors": survivors}
            if survivors:
                validation.report["status"] = "failed"
        except BaseException as error:
            validation.report["final_cleanup"] = {"verified_absent": False, "failure": str(error)}
            validation.report["status"] = "failed"
        validation.save()
        validation.samples.close()
    print(json.dumps({"status": validation.report["status"], "evidence": str(validation.evidence)}))
    return 0 if validation.report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
