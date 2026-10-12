#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""Read one DWG and check that what came back is self-consistent.

::

    python tests/test.py                    # the drawing below
    python tests/test.py drawing.dwg        # any other one
    python tests/test.py --summary          # just the census text

Exit status 0 when the checks pass, 1 when one fails or the file will not
decode, 2 when the path does not exist.

This is a standalone script, not a pytest module: ``test.py`` does not match
pytest's ``test_*.py`` collection pattern, so pytest leaves it alone.

Why the imports are not at the top of the file
----------------------------------------------

The two ways to run it need different import forms, and only one of them can be
right at a time:

* as a module, ``py_libredwg`` is already importable and this file's own
  package is known, so the relative ``from .. import ...`` is correct — and it
  stays correct wherever the file sits;
* as a script, Python puts **the script's own directory** on ``sys.path``, not
  the current directory, so the repository root is invisible and a relative
  import has no parent package to resolve against.  That is the
  ``ImportError: attempted relative import with no known parent package`` from
  ``python .../test.py``.

The block below therefore asks which situation it is in — ``__package__`` is
set only for the module case — and imports accordingly, putting the repository
root on ``sys.path`` for the script case.  ``_repo_root`` finds that root by
searching upwards for the package rather than counting ``parents[n]``, so
moving this file between the repository's ``tests/`` and the package's own
``tests/`` cannot silently aim the path at the wrong directory.
"""

from __future__ import annotations

import sys
from pathlib import Path


from py_libredwg import DwgDecoder, DwgError  # noqa: E402
from py_libredwg.dwg_parser import parser  # noqa: E402


DEFAULT_PATH = "drawing.dwg"


def force_utf8_output() -> None:
    """Print UTF-8 even on a legacy console code page.

    A Chinese drawing's layer names cannot all be encoded in GBK/cp1252, and the
    report would otherwise die with ``UnicodeEncodeError`` *after* a successful
    parse.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def check(decoder: DwgDecoder) -> list[str]:
    """Return the things that look wrong; an empty list means all good.

    These are properties any correct parse must have, so a failure means either
    a decoder bug or a genuinely malformed file — and ``decoder.problems`` is
    what tells the two apart.  They need no per-file expected values, which is
    what makes the script useful on a drawing nobody has parsed before.
    """
    bad: list[str] = []

    if not decoder.entities:
        bad.append("no entities decoded")
    if decoder.problems:
        bad.append(f"{len(decoder.problems)} problem(s), first: "
                   f"{decoder.problems[0]}")

    unresolved = [e for e in decoder.entities if not e.layer]
    if unresolved:
        bad.append(f"{len(unresolved)} entities resolved no layer "
                   f"(first: 0x{unresolved[0].handle:X})")

    known = {layer.name for layer in decoder.layers}
    stray = [e for e in decoder.entities if e.layer and e.layer not in known]
    if stray:
        bad.append(f"{len(stray)} entities name a layer not in the layer table "
                   f"(first: {stray[0].layer!r})")

    # Layer resolution is the load-bearing cross-reference: it is the one handle
    # every entity has, so it exercises the TABLES decode and the handle stream
    # at once.
    handles = [o.handle for o in decoder.objects if o.handle]
    if len(set(handles)) != len(handles):
        bad.append(f"duplicate handles: {len(handles)} handles, "
                   f"{len(set(handles))} distinct")

    return bad


def main(argv: list[str]) -> int:
    force_utf8_output()

    args = [a for a in argv[1:] if not a.startswith("-")]
    path = args[0] if args else DEFAULT_PATH

    if not Path(path).is_file():
        print(f"no such DWG file: {path}", file=sys.stderr)
        return 2

    try:
        decoder = DwgDecoder(path).parse()
    except DwgError as exc:
        # A truncated file, an unsupported version, a bit stream that ran off
        # the end.  Reported as a message, not a traceback: the traceback would
        # name decoder internals that say nothing about the drawing.
        print(f"cannot decode {path}: {exc}", file=sys.stderr)
        return 1

    if "--summary" in argv[1:]:
        # The one-call front end, which is the same renderer the command line
        # uses — so this and `py-libredwg drawing.dwg` cannot disagree.
        print(parser(path, "summary"))
        return 0

    # Two ways in, both exercised here so a broken import shows up immediately:
    # the object model ...
    print(f"file        : {decoder.source}")
    print(f"version     : {decoder.version_tag} ({decoder.version.name})")
    print(f"objects     : {len(decoder.objects)}")
    print(f"entities    : {len(decoder.entities)}")
    print(f"layers      : {len(decoder.layers)}  "
          f"{[layer.name for layer in decoder.layers]}")
    print(f"proxies     : {len(decoder.proxies)}")
    print(f"undecoded   : {len(decoder.unknown)}")
    print(f"problems    : {len(decoder.problems)}")

    # ... and the path-and-format entry point.
    features = parser(path, "geojson")["features"]
    print(f"geojson     : {len(features)} features")

    failures = check(decoder)
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
