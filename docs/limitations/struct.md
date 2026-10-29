# struct

The `struct` module provides `pack`, `unpack`, `calcsize`, and `error`.

## Unsupported APIs

`Struct`, `pack_into`, `unpack_from`, `iter_unpack`, and `_clearcache` are not available.

## Buffers

Buffers and byte fields accept `bytes`.
Monty does not provide `bytearray` or `memoryview`.

## Numeric conversions

Numeric conversion methods run synchronously.
An external function or OS call cannot suspend inside `__index__`, `__float__`, `__complex__`, or `__bool__`.
See [classes.md](classes.md#divergences-from-cpython).

## Non-ASCII formats

Non-ASCII `bytes` formats raise `UnicodeEncodeError` inside the sandbox.
The exception message matches CPython.
CPython stores surrogate characters in the failing format object, which Monty cannot represent.
Consequently, the host's `MontyError.exception()` returns `ValueError` with the same message for these errors.
The host cannot inspect the original Unicode exception fields.
See [encoding.md](encoding.md).

Non-ASCII `str` formats use the existing structured Unicode error transport, subject to its object size limit.
