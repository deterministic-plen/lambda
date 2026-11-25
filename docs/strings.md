# Strings, bytes, and Unicode

A Lambda `String` is a length-tagged byte buffer.  String literals produce
UTF-8 bytes, while `read-file` reads a regular file and preserves its bytes
exactly (the string is empty on error; directories and other non-regular files
are rejected by the PR 2.2 fix).  This is intentional: a lexer must be able to
inspect input that is not valid UTF-8 without losing bytes.

## Lossless access

```lambda
s → "A😀é"

(str-byte-length s)     ; 7
(str-byte-at s 0)       ; 65
(str-byte-at s 1)       ; 240
(str-byte-at s 999)     ; -1
```

`str-byte-at` and `str-byte-length` are **non-consuming** and bounds-safe.
`lib/string.lambda` also provides the shorter `(str-at s i)` for the Unicode
scalar at scalar index `i`, and `(str-bytes-at s i)` for byte `i`.
`str-len` remains the older consuming byte-length operation (it takes ownership
of the string; prefer `str-byte-length` for inspection).

## Unicode scalar access

```lambda
(str-codepoint-length s)   ; 3
(str-codepoint-at s 0)     ; 65
(str-codepoint-at s 1)     ; 128512 (😀)
(str-codepoint-at s 2)     ; 233 (é)
(str-utf8-at s 1)          ; 128512 — byte offset -> scalar
(str-valid-utf8 s)         ; true / false
```

Invalid UTF-8 bytes are exposed as `-1`; they count as one position for
`str-codepoint-at`/`str-codepoint-length`.  `str-utf8-at` takes a **byte**
offset; `str-codepoint-at` takes a **scalar index**.

Ruby-style `string[a]` indexing is supported when the `[` is adjacent to a
string-valued name: `s[2]` is `(str-codepoint-at s 2)` and returns the Unicode
codepoint at **scalar index** 2, or `-1` when out of range.  It is deliberately
the codepoint operation, matching `str-codepoint-at`; use `str-byte-at` and
`str-utf8-at` when an FSM needs byte or UTF-8 offsets.  A space before `[`
keeps ordinary application (`f [1, 2]` passes a list).

## UTF-16

Two representations are available:

```lambda
; ArrayI of UTF-16 code units
u → (str->utf16le "A😀")
(arrayi-length u)          ; 3
(utf16le->str u)           ; "A😀"

; ArrayB of UTF-16LE bytes (e.g. from a Windows text file)
b → (read-file-bytes path)
s → (utf16le-bytes->str b)
(bytes → (str->utf16le-bytes s))
```

Invalid/unpaired surrogate code units decode to U+FFFD.  For arbitrary binary
files use `read-file-bytes` and keep the `ArrayB`; `str->arrayb` / `arrayb->str`
remain lossless byte conversions.

## Integer conversion

`str->int` is permissive: it saturates on overflow
(`"99999999999999999999999"` becomes `9223372036854775807`), accepts trailing
junk (`"12abc"` becomes 12, `"abc"` becomes 0), and has no failure signal, so
`0` cannot be distinguished from a parse failure (PR 3B).  Validate the input
explicitly if the difference matters.

## FSMs and membership

`str-byte-at` returns an `Int`, so state machines can compare bytes without
allocating one-character strings:

```lambda
include "lib/list.lambda"
include "lib/string.lambda"

digit? → (λ s: String i: Int.
  (ascii-digit? (str-byte-at s i)))

is-ident-start? → (λ s: String i: Int.
  c → (str-byte-at s i)
  (or (ascii-alpha? c) (if (in? c [95]) then true else false)))
```

`member` / `in?` are list membership tests, the concise equivalent of
`x in (a, b, c)` for finite alternatives:

```lambda
(member c [43, 45])        ; + or -
(in? c [43, 45])
```

`member` uses structural equality, so it also works for strings and enums.

Lists are homogeneous.  A list that mixes an `Int` and the `Bool` result of
`str-valid-utf8` does not type-check (it used to silently select the boxed
fallback); keep the boolean out of integer lists, or use a sum/product type when
values of different types really belong together.
