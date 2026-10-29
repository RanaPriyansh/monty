//! Binary packing with CPython's native and standard format layouts.

use std::{
    ffi::{c_double, c_float, c_int, c_long, c_longlong, c_short},
    fmt::Display,
    mem::{align_of, size_of},
};

use half::f16;
use monty_types::{ResourceError, ResourceTracker};
use num_traits::{Signed, ToPrimitive};

use crate::{
    args::{ArgValues, FromArgs},
    builtins::Builtins,
    bytecode::VM,
    defer_drop,
    exception_private::{ExcType, ExcTypeExt, RunError, RunResult, SimpleException},
    heap::{DropGuard, HEAP_ENTRY_SIZE, HeapData, HeapId},
    intern::StaticStrings,
    modules::ModuleFunctions,
    resource_checks::check_estimated_size,
    types::{
        Complex, LongInt, Module, PyTrait, allocate_tuple, bytes::allocate_bytes, instance::instance_call_dunder_sync,
        tuple::TupleVec,
    },
    value::{VALUE_SIZE, Value},
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, strum::Display, serde::Serialize, serde::Deserialize)]
#[strum(serialize_all = "lowercase")]
pub(crate) enum StructFunctions {
    Pack,
    Unpack,
    Calcsize,
}

pub fn create_module(vm: &mut VM<'_>) -> HeapId {
    let mut module = Module::new(StaticStrings::Struct, vm.interns);
    module.set_attr(
        StaticStrings::Error,
        Value::Builtin(Builtins::ExcType(ExcType::StructError)),
        vm,
    );
    for (name, function) in [
        (StaticStrings::Pack, StructFunctions::Pack),
        (StaticStrings::Unpack, StructFunctions::Unpack),
        (StaticStrings::Calcsize, StructFunctions::Calcsize),
    ] {
        module.set_attr(name, Value::ModuleFunction(ModuleFunctions::Struct(function)), vm);
    }
    vm.heap.allocate(HeapData::Module(Box::new(module)))
}

pub(super) fn call(vm: &mut VM<'_>, function: StructFunctions, args: ArgValues) -> RunResult<Value> {
    match function {
        StructFunctions::Pack => pack(vm, args),
        StructFunctions::Unpack => unpack(vm, args),
        StructFunctions::Calcsize => {
            let format = args
                .reject_kwargs("_struct.calcsize", vm.heap)?
                .get_one_arg("calcsize", vm.heap)?;
            defer_drop!(format, vm);
            let layout = layout(format, false, vm)?;
            Ok(Value::Int(
                i64::try_from(layout.size).expect("layout fits signed length"),
            ))
        }
    }
}

#[derive(FromArgs)]
#[from_args(name = "pack")]
struct PackArgs {
    #[from_args(pos_only)]
    format: Value,
    #[from_args(varargs)]
    values: Vec<Value>,
}

#[derive(Clone, Copy)]
struct Segment {
    code: u8,
    offset: usize,
    width: usize,
    count: usize,
}

struct Layout {
    native: bool,
    little: bool,
    size: usize,
    items: usize,
    segments: Vec<Segment>,
}

fn struct_error(message: impl Display) -> RunError {
    SimpleException::new_msg(ExcType::StructError, message).into()
}

fn checked_size(value: Option<usize>) -> RunResult<usize> {
    value
        .filter(|value| *value <= isize::MAX.unsigned_abs())
        .ok_or_else(|| struct_error("total struct size too long"))
}

fn bytes<'a>(value: &Value, vm: &'a VM<'_>) -> Option<&'a [u8]> {
    match value {
        Value::InternBytes(id) => Some(vm.interns.get_bytes(*id)),
        Value::Ref(id) => match vm.heap.get(*id) {
            HeapData::Bytes(value) => Some(value.as_slice()),
            _ => None,
        },
        _ => None,
    }
}

const FORMAT_SCAN_CHUNK: usize = 4096;

fn non_ascii_start(input: &[u8], tracker: &ResourceTracker) -> Result<Option<usize>, ResourceError> {
    for (index, chunk) in input.chunks(FORMAT_SCAN_CHUNK).enumerate() {
        tracker.check_time()?;
        if let Some(offset) = chunk.iter().position(|byte| !byte.is_ascii()) {
            return Ok(Some(index * FORMAT_SCAN_CHUNK + offset));
        }
    }
    Ok(None)
}

fn non_ascii_end(input: &[u8], tracker: &ResourceTracker) -> Result<usize, ResourceError> {
    for (index, chunk) in input.chunks(FORMAT_SCAN_CHUNK).enumerate() {
        tracker.check_time()?;
        if let Some(offset) = chunk.iter().position(u8::is_ascii) {
            return Ok(index * FORMAT_SCAN_CHUNK + offset);
        }
    }
    Ok(input.len())
}

fn unicode_range_length(input: &str, tracker: &ResourceTracker) -> Result<usize, ResourceError> {
    let mut count = 0;
    for (index, _) in input.chars().enumerate() {
        tracker.check_time_every(index)?;
        count = index + 1;
    }
    Ok(count)
}

fn has_nul(input: &[u8], tracker: &ResourceTracker) -> Result<bool, ResourceError> {
    for chunk in input.chunks(FORMAT_SCAN_CHUNK) {
        tracker.check_time()?;
        if chunk.contains(&0) {
            return Ok(true);
        }
    }
    Ok(false)
}

fn format_bytes<'a>(value: &Value, vm: &'a VM<'_>) -> RunResult<&'a [u8]> {
    let tracker = &vm.heap.tracker;
    if let Some(value) = bytes(value, vm) {
        if let Some(start) = non_ascii_start(value, tracker)? {
            let end = start + non_ascii_end(&value[start..], tracker)?;
            let message = if end == start + 1 {
                format!(
                    "'ascii' codec can't encode character '\\u{:04x}' in position {start}: ordinal not in range(128)",
                    0xdc00 + u16::from(value[start])
                )
            } else {
                format!(
                    "'ascii' codec can't encode characters in position {start}-{}: ordinal not in range(128)",
                    end - 1
                )
            };
            return Err(SimpleException::new_msg(ExcType::UnicodeEncodeError, message).into());
        }
        return Ok(value);
    }
    let text = match value {
        Value::InternString(id) => Some(vm.interns.get_str(*id)),
        Value::Ref(id) => match vm.heap.get(*id) {
            HeapData::Str(value) => Some(value.as_str()),
            _ => None,
        },
        _ => None,
    };
    if let Some(text) = text {
        if let Some(start) = non_ascii_start(text.as_bytes(), tracker)? {
            let byte_end = start + non_ascii_end(&text.as_bytes()[start..], tracker)?;
            let range = &text[start..byte_end];
            let first = range.chars().next().expect("nonempty non-ASCII range");
            let end = start + unicode_range_length(range, tracker)?;
            return Err(ExcType::unicode_encode_error(
                "ascii",
                text,
                first,
                start,
                end,
                "ordinal not in range(128)",
            ));
        }
        return Ok(text.as_bytes());
    }
    Err(ExcType::type_error(format!(
        "Struct() argument 1 must be a str or bytes object, not {}",
        value.py_type_name(vm)
    )))
}

fn layout(format: &Value, keep_segments: bool, vm: &mut VM<'_>) -> RunResult<Layout> {
    if !format.is_str(vm.heap) && bytes(format, vm).is_none() && format.py_hash(vm)?.is_none() {
        return Err(ExcType::type_error_unhashable(&format.py_type_name(vm)));
    }
    let format = format_bytes(format, vm)?;
    if has_nul(format, &vm.heap.tracker)? {
        return Err(struct_error("embedded null character"));
    }
    let prefix = format.first().copied().unwrap_or(b'@');
    let native = !matches!(prefix, b'=' | b'<' | b'>' | b'!');
    let little = match prefix {
        b'<' => true,
        b'>' | b'!' => false,
        _ => cfg!(target_endian = "little"),
    };
    let mut index = usize::from(matches!(prefix, b'@' | b'=' | b'<' | b'>' | b'!'));
    let mut result = Layout {
        native,
        little,
        size: 0,
        items: 0,
        segments: Vec::new(),
    };
    let mut steps = 0;
    while index < format.len() {
        vm.heap.tracker.check_memory_time_every(steps)?;
        steps += 1;
        let mut code = format[index];
        index += 1;
        if matches!(code, b' ' | b'\t' | b'\n' | b'\r' | 11 | 12) {
            continue;
        }
        let mut count = 1usize;
        if code.is_ascii_digit() {
            count = 0;
            loop {
                count = checked_size(
                    count
                        .checked_mul(10)
                        .and_then(|n| n.checked_add(usize::from(code - b'0'))),
                )?;
                let Some(next) = format.get(index).copied() else {
                    return Err(struct_error("repeat count given without format specifier"));
                };
                code = next;
                index += 1;
                vm.heap.tracker.check_memory_time_every(steps)?;
                steps += 1;
                if !code.is_ascii_digit() {
                    break;
                }
            }
        }
        let (width, alignment) = field_layout(code, native)?;
        let padding = (alignment - result.size % alignment) % alignment;
        result.size = checked_size(result.size.checked_add(padding))?;
        let offset = result.size;
        result.size = checked_size(count.checked_mul(width).and_then(|n| result.size.checked_add(n)))?;
        let fields = match code {
            b'x' => 0,
            b's' | b'p' => 1,
            _ => count,
        };
        result.items = checked_size(result.items.checked_add(fields))?;
        if keep_segments && fields != 0 {
            vm.heap
                .tracker
                .check_growth(result.segments.len(), result.segments.capacity(), size_of::<Segment>())?;
            result.segments.push(Segment {
                code,
                offset,
                width: if matches!(code, b's' | b'p') { count } else { width },
                count: fields,
            });
        }
    }
    Ok(result)
}

fn field_layout(code: u8, native: bool) -> RunResult<(usize, usize)> {
    let standard = match code {
        b'x' | b'c' | b'b' | b'B' | b'?' | b's' | b'p' => 1,
        b'h' | b'H' | b'e' => 2,
        b'i' | b'I' | b'l' | b'L' | b'f' => 4,
        b'q' | b'Q' | b'd' | b'F' => 8,
        b'D' => 16,
        b'n' | b'N' | b'P' if native => size_of::<usize>(),
        _ => return Err(struct_error("bad char in struct format")),
    };
    if !native {
        return Ok((standard, 1));
    }
    Ok(match code {
        b'h' | b'H' => (size_of::<c_short>(), align_of::<c_short>()),
        b'e' => (2, align_of::<c_short>()),
        b'i' | b'I' => (size_of::<c_int>(), align_of::<c_int>()),
        b'l' | b'L' => (size_of::<c_long>(), align_of::<c_long>()),
        b'q' | b'Q' => (size_of::<c_longlong>(), align_of::<c_longlong>()),
        b'n' | b'N' => (size_of::<usize>(), align_of::<usize>()),
        b'P' => (size_of::<*const ()>(), align_of::<*const ()>()),
        b'f' => (size_of::<c_float>(), align_of::<c_float>()),
        b'd' => (size_of::<c_double>(), align_of::<c_double>()),
        b'F' => (2 * size_of::<c_float>(), align_of::<c_float>()),
        b'D' => (2 * size_of::<c_double>(), align_of::<c_double>()),
        _ => (standard, 1),
    })
}

fn pack(vm: &mut VM<'_>, args: ArgValues) -> RunResult<Value> {
    let args = args.reject_kwargs("_struct.pack", vm.heap)?;
    if args.count() == 0 {
        return Err(ExcType::type_error("missing format argument"));
    }
    let PackArgs { format, values } = PackArgs::from_args(args, vm)?;
    defer_drop!(format, vm);
    defer_drop!(values, vm);
    let layout = layout(format, true, vm)?;
    if layout.items != values.len() {
        return Err(struct_error(format!(
            "pack expected {} items for packing (got {})",
            layout.items,
            values.len()
        )));
    }
    check_estimated_size(layout.size, &vm.heap.tracker)?;
    vm.heap.tracker.check_allocation(layout.size)?;
    let mut output = vec![0; layout.size];
    let mut value_index = 0;
    for segment in &layout.segments {
        for field in 0..segment.count {
            vm.heap.tracker.check_memory_time_every(value_index)?;
            let start = segment.offset + field * segment.width;
            pack_field(
                segment.code,
                &values[value_index],
                &mut output[start..start + segment.width],
                &layout,
                vm,
            )?;
            value_index += 1;
        }
    }
    Ok(allocate_bytes(output, vm.heap))
}

fn unpack(vm: &mut VM<'_>, args: ArgValues) -> RunResult<Value> {
    let (format, buffer) = args
        .reject_kwargs("_struct.unpack", vm.heap)?
        .get_two_args("unpack", vm.heap)?;
    defer_drop!(format, vm);
    defer_drop!(buffer, vm);
    let layout = layout(format, true, vm)?;
    let buffer_len = bytes(buffer, vm)
        .ok_or_else(|| {
            ExcType::type_error(format!(
                "a bytes-like object is required, not '{}'",
                buffer.py_type_name(vm)
            ))
        })?
        .len();
    if buffer_len != layout.size {
        return Err(struct_error(format!(
            "unpack requires a buffer of {} bytes",
            layout.size
        )));
    }
    let estimated = decoded_allocation(&layout);
    if estimated > isize::MAX.unsigned_abs() {
        return Err(ResourceError::Memory {
            limit: isize::MAX.unsigned_abs(),
            used: estimated,
        }
        .into());
    }
    check_estimated_size(estimated, &vm.heap.tracker)?;
    vm.heap.tracker.check_allocation(estimated)?;
    let mut result = DropGuard::new(TupleVec::with_capacity(layout.items), vm);
    let (items, vm) = result.as_parts_mut();
    for segment in &layout.segments {
        for field in 0..segment.count {
            vm.heap.tracker.check_memory_time_every(items.len())?;
            let start = segment.offset + field * segment.width;
            let input = bytes(buffer, vm).expect("buffer already validated");
            let value = unpack_field(segment.code, &input[start..start + segment.width], layout.little, vm);
            items.push(value);
        }
    }
    let (items, vm) = result.into_parts();
    Ok(allocate_tuple(items, vm.heap))
}

fn decoded_allocation(layout: &Layout) -> usize {
    let tuple = layout.items.saturating_mul(VALUE_SIZE).saturating_add(HEAP_ENTRY_SIZE);
    layout.segments.iter().fold(tuple, |total, segment| {
        let item = match segment.code {
            b'c' | b's' | b'p' => HEAP_ENTRY_SIZE.saturating_add(segment.width),
            b'F' | b'D' => HEAP_ENTRY_SIZE,
            b'Q' | b'N' | b'P' | b'L' if segment.width == 8 => HEAP_ENTRY_SIZE + size_of::<u64>(),
            _ => 0,
        };
        total.saturating_add(segment.count.saturating_mul(item))
    })
}

fn range_error(code: u8, minimum: i128, maximum: i128) -> RunError {
    struct_error(format!(
        "'{}' format requires {minimum} <= number <= {maximum}",
        char::from(code)
    ))
}

fn integer(value: &Value, code: u8, width: usize, layout: &Layout, vm: &mut VM<'_>) -> RunResult<u64> {
    let bits = width * 8;
    let signed = matches!(code, b'b' | b'h' | b'i' | b'l' | b'q' | b'n');
    let minimum = if signed || code == b'P' {
        -(1i128 << (bits - 1))
    } else {
        0
    };
    let maximum = if signed {
        (1i128 << (bits - 1)) - 1
    } else {
        (1i128 << bits) - 1
    };
    let index = match value.py_index_impl(vm) {
        Err(error)
            if error.is_catchable(ExcType::OverflowError)
                && code != b'P'
                && (!matches!(code, b'q' | b'Q')
                    || layout.native
                    || layout.little == cfg!(target_endian = "little")) =>
        {
            return Err(range_error(code, minimum, maximum));
        }
        result => result?,
    }
    .ok_or_else(|| struct_error("required argument is not an integer"))?;
    defer_drop!(index, vm);
    let number = match index {
        Value::Int(value) => Some(i128::from(*value)),
        Value::Bool(value) => Some(i128::from(*value)),
        _ => index.as_long_int(vm).and_then(ToPrimitive::to_i128),
    };
    let number = number
        .filter(|number| *number >= minimum && *number <= maximum)
        .ok_or_else(|| {
            if code == b'P' {
                if matches!(value, Value::Int(_) | Value::Bool(_) | Value::InternLongInt(_))
                    || value.as_long_int(vm).is_some()
                {
                    struct_error("int too large to convert")
                } else if size_of::<*const ()>() > size_of::<c_long>() {
                    ExcType::overflow_error("int too big to convert")
                } else if number.is_some_and(|number| number < 0)
                    || index.as_long_int(vm).is_some_and(Signed::is_negative)
                {
                    ExcType::overflow_error("Python int too large to convert to C long")
                } else {
                    ExcType::overflow_error("Python int too large to convert to C unsigned long")
                }
            } else {
                range_error(code, minimum, maximum)
            }
        })?;
    let unsigned = if number < 0 { number + (1i128 << bits) } else { number };
    Ok(u64::try_from(unsigned).expect("field fits u64"))
}

fn numeric_float(value: &Value, vm: &mut VM<'_>) -> RunResult<f64> {
    match value.py_float(vm) {
        Ok(Some(value)) => Ok(value),
        Ok(None) | Err(RunError::Exc(_)) => Err(struct_error("required argument is not a float")),
        Err(error) => Err(error),
    }
}

fn numeric_complex(value: &Value, vm: &mut VM<'_>) -> RunResult<Complex> {
    let converted = (|| {
        if let Value::Ref(id) = value {
            match vm.heap.get(*id) {
                HeapData::Complex(value) => return Ok(*value),
                HeapData::Instance(_) => {
                    if let Some(result) = instance_call_dunder_sync(*id, "__complex__", None, vm)? {
                        defer_drop!(result, vm);
                        if let Value::Ref(id) = result
                            && let HeapData::Complex(value) = vm.heap.get(*id)
                        {
                            return Ok(*value);
                        }
                        return Err(struct_error("required argument is not a complex"));
                    }
                }
                _ => {}
            }
        }
        value
            .py_float(vm)?
            .map(|real| Complex::new(real, 0.0))
            .ok_or_else(|| struct_error("required argument is not a complex"))
    })();
    match converted {
        Err(RunError::Exc(_)) => Err(struct_error("required argument is not a complex")),
        result => result,
    }
}

fn truth(value: &Value, vm: &mut VM<'_>) -> RunResult<bool> {
    let Value::Ref(id) = value else {
        return value.py_bool(vm);
    };
    if !matches!(vm.heap.get(*id), HeapData::Instance(_)) {
        return value.py_bool(vm);
    }
    if let Some(result) = instance_call_dunder_sync(*id, "__bool__", None, vm)? {
        defer_drop!(result, vm);
        return if let Value::Bool(result) = result {
            Ok(*result)
        } else {
            Err(ExcType::type_error(format!(
                "__bool__ should return bool, returned {}",
                result.py_type_name(vm)
            )))
        };
    }
    if let Some(result) = instance_call_dunder_sync(*id, "__len__", None, vm)? {
        defer_drop!(result, vm);
        let index = result.py_index(vm)?;
        defer_drop!(index, vm);
        let negative = match index {
            Value::Int(value) => *value < 0,
            Value::Bool(_) => false,
            _ => index.as_long_int(vm).is_some_and(Signed::is_negative),
        };
        if negative {
            return Err(ExcType::value_error("__len__() should return >= 0"));
        }
        let length = match index {
            Value::Int(value) => usize::try_from(*value).ok(),
            Value::Bool(value) => Some(usize::from(*value)),
            _ => index.as_long_int(vm).and_then(ToPrimitive::to_usize),
        }
        .filter(|value| *value <= isize::MAX.unsigned_abs())
        .ok_or_else(ExcType::overflow_index_sized_int)?;
        Ok(length != 0)
    } else {
        Ok(true)
    }
}

fn write_integer(number: u64, output: &mut [u8], little: bool) {
    let encoded = if little {
        number.to_le_bytes()
    } else {
        number.to_be_bytes()
    };
    let encoded = if little {
        &encoded[..output.len()]
    } else {
        &encoded[8 - output.len()..]
    };
    output.copy_from_slice(encoded);
}

fn pack_float(number: f64, output: &mut [u8], little: bool, checked: bool) -> RunResult<()> {
    let encoded = match output.len() {
        2 => {
            let half = f16::from_f64(number);
            if number.is_finite() && half.is_infinite() {
                return Err(ExcType::overflow_error("float too large to pack with e format"));
            }
            let bits = if number.is_nan() {
                if number.is_sign_negative() { 0xfe00 } else { 0x7e00 }
            } else {
                half.to_bits()
            };
            u64::from(bits)
        }
        4 => {
            let single = number.to_f32().unwrap_or_else(|| {
                if number.is_sign_negative() {
                    f32::NEG_INFINITY
                } else {
                    f32::INFINITY
                }
            });
            if checked && number.is_finite() && single.is_infinite() {
                return Err(ExcType::overflow_error("float too large to pack with f format"));
            }
            u64::from(single.to_bits())
        }
        _ => number.to_bits(),
    };
    write_integer(encoded, output, little);
    Ok(())
}

fn pack_field(code: u8, value: &Value, output: &mut [u8], layout: &Layout, vm: &mut VM<'_>) -> RunResult<()> {
    match code {
        b'c' | b's' | b'p' => {
            let input = bytes(value, vm).ok_or_else(|| {
                struct_error(match code {
                    b'c' => "char format requires a bytes object of length 1",
                    b's' => "argument for 's' must be a bytes object",
                    _ => "argument for 'p' must be a bytes object",
                })
            })?;
            if code == b'c' && input.len() != 1 {
                return Err(struct_error("char format requires a bytes object of length 1"));
            }
            if code == b'p' {
                if let Some((length, target)) = output.split_first_mut() {
                    let count = input.len().min(target.len());
                    target[..count].copy_from_slice(&input[..count]);
                    *length = u8::try_from(count.min(255)).expect("length is capped at 255");
                }
            } else {
                let count = input.len().min(output.len());
                output[..count].copy_from_slice(&input[..count]);
            }
        }
        b'?' => output[0] = u8::from(truth(value, vm)?),
        b'e' | b'f' | b'd' => pack_float(numeric_float(value, vm)?, output, layout.little, true)?,
        b'F' | b'D' => {
            let value = numeric_complex(value, vm)?;
            let (real, imag) = output.split_at_mut(output.len() / 2);
            let checked = !layout.native && layout.little != cfg!(target_endian = "little");
            pack_float(value.real, real, layout.little, checked)?;
            pack_float(value.imag, imag, layout.little, checked)?;
        }
        _ => write_integer(integer(value, code, output.len(), layout, vm)?, output, layout.little),
    }
    Ok(())
}

fn read_integer(input: &[u8], little: bool) -> u64 {
    let mut buffer = [0; 8];
    if little {
        buffer[..input.len()].copy_from_slice(input);
        u64::from_le_bytes(buffer)
    } else {
        buffer[8 - input.len()..].copy_from_slice(input);
        u64::from_be_bytes(buffer)
    }
}

fn read_float(input: &[u8], little: bool) -> f64 {
    let bits = read_integer(input, little);
    match input.len() {
        2 => {
            let half = f16::from_bits(u16::try_from(bits).expect("two bytes"));
            if half.is_nan() {
                f64::from_bits(0x7ff8_0000_0000_0000 | ((bits & 0x8000) << 48))
            } else {
                half.to_f64()
            }
        }
        4 => f64::from(f32::from_bits(u32::try_from(bits).expect("four bytes"))),
        _ => f64::from_bits(bits),
    }
}

fn unpack_field(code: u8, input: &[u8], little: bool, vm: &VM<'_>) -> Value {
    match code {
        b'c' | b's' => allocate_bytes(input.to_vec(), vm.heap),
        b'p' => {
            let count = input
                .first()
                .map_or(0, |length| usize::from(*length).min(input.len() - 1));
            allocate_bytes(input.get(1..1 + count).unwrap_or_default().to_vec(), vm.heap)
        }
        b'?' => Value::Bool(input.iter().any(|byte| *byte != 0)),
        b'e' | b'f' | b'd' => Value::Float(read_float(input, little)),
        b'F' | b'D' => {
            let (real, imag) = input.split_at(input.len() / 2);
            Complex::new(read_float(real, little), read_float(imag, little)).into_value(vm.heap)
        }
        _ => {
            let number = read_integer(input, little);
            if matches!(code, b'b' | b'h' | b'i' | b'l' | b'q' | b'n') {
                let bits = input.len() * 8;
                let signed = if number & (1u64 << (bits - 1)) != 0 {
                    i128::from(number) - (1i128 << bits)
                } else {
                    i128::from(number)
                };
                Value::Int(i64::try_from(signed).expect("signed field fits i64"))
            } else {
                LongInt::value_from_u128(u128::from(number), vm.heap)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::{thread, time::Duration};

    use monty_types::{ResourceError, ResourceLimits, ResourceTracker};

    use super::{FORMAT_SCAN_CHUNK, has_nul, non_ascii_end, non_ascii_start, unicode_range_length};

    #[test]
    fn format_traversals_check_an_expired_tracker_without_a_vm_boundary() {
        let tracker = ResourceTracker::new(ResourceLimits::default().max_feed_duration(Duration::ZERO));
        tracker.on_execution_start();
        thread::sleep(Duration::from_millis(1));
        tracker.on_execution_stop();
        assert!(matches!(tracker.check_time(), Err(ResourceError::Time { .. })));

        let ascii = vec![b'x'; FORMAT_SCAN_CHUNK * 2];
        let non_ascii = vec![0xff; FORMAT_SCAN_CHUNK * 2];
        let unicode = "é😀".repeat(ResourceTracker::LOOP_CHECK_INTERVAL);
        assert!(matches!(
            non_ascii_start(&ascii, &tracker),
            Err(ResourceError::Time { .. })
        ));
        assert!(matches!(
            non_ascii_end(&non_ascii, &tracker),
            Err(ResourceError::Time { .. })
        ));
        assert!(matches!(
            unicode_range_length(&unicode, &tracker),
            Err(ResourceError::Time { .. })
        ));
        assert!(matches!(has_nul(&ascii, &tracker), Err(ResourceError::Time { .. })));
    }
}
