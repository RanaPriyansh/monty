use std::{env, mem::size_of, process};

use monty_pool::{Pool, PoolConfig, PoolError, ReplConfig, TurnEvent};
use monty_proto::{DEFAULT_MAX_DECODE_BYTES, MAX_FRAME_LEN};
use monty_types::{PrintStream, unstable::MontyNode};

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let result = run().await;
    if let Err(message) = result {
        eprintln!("FAIL: {message}");
        process::exit(1);
    }
}

async fn run() -> Result<(), String> {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.first().is_some_and(|arg| arg == "metadata") && args.len() == 1 {
        return print_metadata();
    }
    if args.len() != 3 {
        return Err("usage: witness metadata | reject WORKER_PATH M | neighbor WORKER_PATH N".to_owned());
    }

    let mode = &args[0];
    let worker_path = &args[1];
    let count = args[2]
        .parse::<usize>()
        .map_err(|_| "count must be a nonnegative usize".to_owned())?;
    if mode != "reject" && mode != "neighbor" {
        return Err("mode must be metadata, reject, or neighbor".to_owned());
    }

    let mut config = PoolConfig::subprocess(worker_path.as_str());
    config.min_processes = 0;
    config.max_processes = 1;
    let pool = Pool::new(config)
        .await
        .map_err(|error| format!("pool startup failed: {}", bounded(&error.to_string())))?;
    let result = exercise(&pool, mode, count).await;
    pool.close().await;
    result
}

fn print_metadata() -> Result<(), String> {
    let node_bytes = size_of::<MontyNode>();
    let usize_bytes = size_of::<usize>();
    let threshold = first_over_budget(node_bytes, usize_bytes, DEFAULT_MAX_DECODE_BYTES)
        .ok_or_else(|| "could not compute threshold with checked arithmetic".to_owned())?;
    let below = decode_charge(threshold - 1, node_bytes, usize_bytes)
        .ok_or_else(|| "threshold predecessor charge overflowed".to_owned())?;
    let above =
        decode_charge(threshold, node_bytes, usize_bytes).ok_or_else(|| "threshold charge overflowed".to_owned())?;
    println!("node_bytes={node_bytes}");
    println!("usize_bytes={usize_bytes}");
    println!("default_max_decode_bytes={DEFAULT_MAX_DECODE_BYTES}");
    println!("max_frame_len={MAX_FRAME_LEN}");
    println!("first_over_budget_m={threshold}");
    println!("charge_m_minus_1={below}");
    println!("charge_m={above}");
    Ok(())
}

fn first_over_budget(node_bytes: usize, usize_bytes: usize, budget: usize) -> Option<usize> {
    if decode_charge(0, node_bytes, usize_bytes).is_none_or(|charge| charge > budget) {
        return None;
    }
    let mut high = 1usize;
    while decode_charge(high, node_bytes, usize_bytes).is_some_and(|charge| charge <= budget) {
        high = high.checked_mul(2)?;
    }
    let mut low = 0usize;
    while low + 1 < high {
        let middle = low + (high - low) / 2;
        if decode_charge(middle, node_bytes, usize_bytes).is_some_and(|charge| charge <= budget) {
            low = middle;
        } else {
            high = middle;
        }
    }
    Some(high)
}

fn decode_charge(m: usize, node_bytes: usize, usize_bytes: usize) -> Option<usize> {
    let node_count = m.checked_add(1)?;
    let node_charge = node_count.checked_mul(node_bytes)?;
    let id_varint_bytes = varint_bytes_for_ids(m)?;
    let list_refs = id_varint_bytes.checked_mul(2)?.checked_mul(usize_bytes)?;
    node_charge.checked_add(list_refs)
}

fn varint_bytes_for_ids(m: usize) -> Option<usize> {
    let mut total = 0usize;
    let mut start = 0usize;
    let mut width = 1u32;
    while start < m {
        let boundary = 1usize.checked_shl(width.checked_mul(7)?)?;
        let end = m.min(boundary);
        let count = end.checked_sub(start)?;
        total = total.checked_add(count.checked_mul(width as usize)?)?;
        start = end;
        width = width.checked_add(1)?;
    }
    Some(total)
}

async fn exercise(pool: &Pool, mode: &str, count: usize) -> Result<(), String> {
    let mut checkout = pool
        .checkout(&ReplConfig::default())
        .await
        .map_err(|error| format!("checkout failed: {}", bounded(&error.to_string())))?;
    let result = match mode {
        "reject" => reject(&mut checkout, count).await,
        "neighbor" => neighbor(&mut checkout, count).await,
        _ => Err("unreachable mode".to_owned()),
    };
    let finish_result = checkout
        .finish()
        .await
        .map_err(|error| format!("checkout cleanup failed: {}", bounded(&error.to_string())));
    match (result, finish_result) {
        (Err(primary), Err(cleanup)) => {
            eprintln!("cleanup_failure={cleanup}");
            Err(primary)
        }
        (Err(primary), Ok(())) => {
            println!("checkout_cleanup=completed");
            Err(primary)
        }
        (Ok(()), cleanup) => cleanup,
    }
}

async fn reject(checkout: &mut monty_pool::Checkout, count: usize) -> Result<(), String> {
    expect_int(
        checkout
            .feed("sentinel = 17\n42", vec![], vec![], false, &mut no_print)
            .await
            .map_err(|error| format!("initial sentinel failed: {}", bounded(&error.to_string())))?,
        42,
        "initial sentinel",
    )?;
    let identity_before = checkout
        .worker_id()
        .ok_or_else(|| "worker identity was absent before rejection".to_owned())?;

    let (first_failure_variant, first_failure) = match checkout
        .feed(format!("list(range({count}))"), vec![], vec![], false, &mut no_print)
        .await
    {
        Ok(TurnEvent::Complete(value)) => {
            drop(value);
            ("Complete", "unexpected Complete".to_owned())
        }
        Ok(other) => ("Event", format!("unexpected event: {}", event_name(&other))),
        Err(PoolError::Runtime(exception)) => (
            "Runtime",
            bounded(exception.message().unwrap_or("Runtime error without a message")),
        ),
        Err(error) => (error_name(&error), bounded(&error.to_string())),
    };
    println!("first_failure_variant={first_failure_variant}");
    println!("first_failure={first_failure}");

    let identity_after = checkout.worker_id();
    let after_error = match checkout.feed("sentinel", vec![], vec![], false, &mut no_print).await {
        Ok(event) => match expect_int(event, 17, "after-error sentinel") {
            Ok(()) => "Complete17".to_owned(),
            Err(message) => message,
        },
        Err(error) => format!("{}: {}", error_name(&error), bounded(&error.to_string())),
    };
    let identity_after_sentinel = checkout.worker_id();
    println!("worker_before={identity_before}");
    println!("worker_after_error={identity_after:?}");
    println!("worker_after_sentinel={identity_after_sentinel:?}");
    println!("after_error={after_error}");

    let error_is_budget = first_failure.contains("decode") && first_failure.contains("budget");
    if first_failure_variant != "Runtime" || !error_is_budget {
        return Err("rejection was not a decode-budget Runtime error".to_owned());
    }
    if identity_after != Some(identity_before) || identity_after_sentinel != Some(identity_before) {
        return Err("worker identity changed or checkout finished after rejection".to_owned());
    }
    if after_error != "Complete17" {
        return Err("same checkout did not complete the sentinel after rejection".to_owned());
    }
    Ok(())
}

async fn neighbor(checkout: &mut monty_pool::Checkout, count: usize) -> Result<(), String> {
    expect_int(
        checkout
            .feed("sentinel = 17\n42", vec![], vec![], false, &mut no_print)
            .await
            .map_err(|error| format!("initial neighbor sentinel failed: {}", bounded(&error.to_string())))?,
        42,
        "initial neighbor sentinel",
    )?;
    let identity_before = checkout
        .worker_id()
        .ok_or_else(|| "worker identity was absent before neighbor result".to_owned())?;
    let event = checkout
        .feed(format!("list(range({count}))"), vec![], vec![], false, &mut no_print)
        .await
        .map_err(|error| format!("neighbor feed failed: {}", bounded(&error.to_string())))?;
    let TurnEvent::Complete(value) = event else {
        return Err(format!("neighbor expected Complete, got {}", event_name(&event)));
    };
    if value.as_ref().type_name() != "list" {
        return Err("neighbor result was not a list".to_owned());
    }
    let items = value
        .as_ref()
        .items()
        .ok_or_else(|| "neighbor result was not a list".to_owned())?;
    if items.len() != count {
        return Err(format!("neighbor list length was {}, expected {count}", items.len()));
    }
    if count == 0 {
        println!("neighbor_values=empty");
    } else {
        for index in [0, count / 2, count - 1] {
            let expected = i64::try_from(index).map_err(|_| "neighbor index exceeds i64".to_owned())?;
            if items[index].as_int() != Some(expected) {
                return Err(format!("neighbor value at index {index} was incorrect"));
            }
        }
        println!("neighbor_values=checked");
    }
    drop(items);
    drop(value);

    let identity_after_list = checkout.worker_id();
    let sentinel = checkout
        .feed("sentinel", vec![], vec![], false, &mut no_print)
        .await
        .map_err(|error| format!("neighbor sentinel failed: {}", bounded(&error.to_string())))?;
    expect_int(sentinel, 17, "neighbor sentinel")?;
    let identity_after_sentinel = checkout.worker_id();
    println!("neighbor_length={count}");
    println!("worker_before={identity_before}");
    println!("worker_after_list={identity_after_list:?}");
    println!("worker_after_sentinel={identity_after_sentinel:?}");
    if identity_after_list != Some(identity_before) || identity_after_sentinel != Some(identity_before) {
        return Err("worker identity changed during successful neighbor feeds".to_owned());
    }
    Ok(())
}

fn expect_int(event: TurnEvent, expected: i64, label: &str) -> Result<(), String> {
    let TurnEvent::Complete(value) = event else {
        return Err(format!("{label} expected Complete, got {}", event_name(&event)));
    };
    let actual = value.as_ref().as_int();
    if actual != Some(expected) {
        return Err(format!("{label} returned {actual:?}, expected {expected}"));
    }
    Ok(())
}

fn event_name(event: &TurnEvent) -> &'static str {
    match event {
        TurnEvent::Complete(_) => "Complete",
        TurnEvent::NameLookup { .. } => "NameLookup",
        TurnEvent::OsCall { .. } => "OsCall",
        TurnEvent::FunctionCall { .. } => "FunctionCall",
        TurnEvent::ResolveFutures { .. } => "ResolveFutures",
    }
}

fn error_name(error: &PoolError) -> &'static str {
    match error {
        PoolError::Crashed { .. } => "Crashed",
        PoolError::Timeout { .. } => "Timeout",
        PoolError::Protocol(_) => "Protocol",
        PoolError::Runtime(_) => "Runtime",
        PoolError::Typing(_) => "Typing",
        PoolError::Exhausted => "Exhausted",
        PoolError::Spawn(_) => "Spawn",
        PoolError::Finished => "Finished",
        PoolError::Disconnected { .. } => "Disconnected",
        PoolError::Shutdown { .. } => "Shutdown",
    }
}

fn bounded(message: &str) -> String {
    const LIMIT: usize = 240;
    let mut end = message.len().min(LIMIT);
    while !message.is_char_boundary(end) {
        end -= 1;
    }
    message[..end].to_owned()
}

fn no_print(_: PrintStream, _: &str) -> monty_pool::PrintFuture {
    Box::pin(std::future::ready(()))
}
