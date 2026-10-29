use std::time::{Duration, Instant};

use monty::{Dump, MontyRepl, Session, SessionRef, dump};
use monty_types::{CompileOptions, ExcType, MontyObject, PrintWriter, ResourceTracker};

#[test]
fn imported_aliases_and_error_survive_a_trusted_dump() {
    let mut repl = MontyRepl::new("struct.py", ResourceTracker::default(), CompileOptions::default());
    repl.feed_run(
        "from struct import pack as p, unpack as u, calcsize as s, error as E",
        vec![],
        PrintWriter::Stdout,
    )
    .unwrap();
    let bytes = dump("struct.py", None, &[], SessionRef::Idle(&repl)).unwrap();
    let Session::Idle(mut restored) = Dump::load(&bytes).unwrap().state else {
        panic!("expected an idle session");
    };
    let result = restored.feed_run("assert s('>I') == 4\nassert u('>I', p('>I', 640)) == (640,)\ntry:\n    p('Z')\nexcept E as e:\n    assert str(e) == 'bad char in struct format'\n17", vec![], PrintWriter::Stdout).unwrap();
    assert_eq!(result, MontyObject::int(17));
}

#[test]
fn prepared_format_scans_observe_the_time_limit() {
    for input in ["'0x' * 10000000", "'é' * 10000000", "b'\\xff' * 10000000"] {
        let mut repl = MontyRepl::new("struct.py", ResourceTracker::default(), CompileOptions::default());
        repl.feed_run(&format!("import struct\nformat = {input}"), vec![], PrintWriter::Stdout)
            .unwrap();
        repl.tracker_mut().set_max_feed_duration(Duration::from_millis(5));
        let start = Instant::now();
        let error = repl
            .feed_run(
                "try:\n    struct.calcsize(format)\nexcept Exception:\n    result = 'caught'",
                vec![],
                PrintWriter::Stdout,
            )
            .expect_err("format scan must stop at the resource limit");
        assert_eq!(error.exc_type(), ExcType::TimeoutError);
        assert!(start.elapsed() < Duration::from_secs(2));
    }
}

#[test]
fn calcsize_keeps_large_repeats_compact_and_checks_arithmetic() {
    let mut repl = MontyRepl::new("struct.py", ResourceTracker::default(), CompileOptions::default());
    let result = repl.feed_run("import struct\nassert struct.calcsize('1000000000B') == 1000000000\ntry:\n    struct.calcsize('999999999999999999999999999999999999B')\nexcept struct.error as e:\n    assert str(e) == 'total struct size too long'\nelse:\n    assert False, 'expected overflow'\n17", vec![], PrintWriter::Stdout).unwrap();
    assert_eq!(result, MontyObject::int(17));
}
