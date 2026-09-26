"""Parsing of the Trade Republic websocket frames.

Frames look like "<subscription id> <code> <payload>":
  A  full JSON answer
  D  delta against the previous answer of the same subscription
  C  subscription closed by the server
  E  error, payload is JSON
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass


@dataclass(frozen=True)
class Frame:
    sub_id: str
    code: str
    body: str


def parse_frame(raw: str) -> Frame | None:
    space = raw.find(" ")
    if space <= 0:
        return None
    code = raw[space + 1 : space + 2]
    if code not in ("A", "D", "C", "E"):
        return None
    return Frame(raw[:space], code, raw[space + 2 :].lstrip())


def apply_delta(previous: str, delta: str) -> str:
    """Rebuild the new JSON text from the previous one and a tab-separated delta.

    "=n" copies n characters of the previous text, "-n" skips n of them and
    "+text" inserts url-encoded text.
    """
    result: list[str] = []
    pos = 0
    for op in delta.split("\t"):
        if not op:
            continue
        sign, arg = op[0], op[1:]
        if sign == "+":
            result.append(urllib.parse.unquote_plus(arg))
        elif sign == "=":
            n = int(arg)
            result.append(previous[pos : pos + n])
            pos += n
        elif sign == "-":
            pos += int(arg)
        else:
            raise ValueError(f"unknown delta instruction {op[:10]!r}")
    return "".join(result)
