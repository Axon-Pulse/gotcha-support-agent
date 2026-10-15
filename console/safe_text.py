"""Checks for text the console writes into line-based files (.env, YAML values).

Python's str.splitlines() breaks a line not only at \\n and \\r but also at \\v, \\f, \\x1c-\\x1e,
\\x85 and U+2028/2029. A value carrying one of those is one line to the code that wrote it and
two to the code that reads it back, which would let a saved value smuggle in a second
NAME=value line. So: values may hold no control or line-separator character at all, and the
files are read by splitting at \\n only.
"""
import unicodedata

_BAD_CATEGORIES = {"Cc", "Zl", "Zp"}  # control characters (C0, DEL, C1), line/paragraph separators


def has_control(s: str) -> bool:
    return any(unicodedata.category(c) in _BAD_CATEGORIES for c in s)


def lines(text: str) -> list[str]:
    """The file's lines, split at \\n only (a \\r before it is dropped, as editors write it)."""
    out = text.split("\n")
    if out and out[-1] == "":
        out.pop()
    return [line[:-1] if line.endswith("\r") else line for line in out]
