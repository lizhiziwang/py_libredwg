# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""Command line front end — ``py-libredwg drawing.dwg``.

The console script declared in ``pyproject.toml``.  It is deliberately thin:
parse, then either print a census or hand the parsed document to one of the
two exporters.  Anything cleverer belongs in a caller's own script, which is
what :mod:`py_libredwg.exporters` is public for.

Three output shapes, chosen with ``--format`` (or inferred from ``-o``'s
suffix):

``summary``   (default) A short human-readable census — version, object and
              entity counts, the layer table, and the parse problems.
``geojson``   The ``FeatureCollection`` from ``out_geojson.c``.
``json``      The plain structured export, for inspecting a parse.

Examples
--------

::

    py-libredwg drawing.dwg
    py-libredwg drawing.dwg -o drawing.geojson
    py-libredwg drawing.dwg --format json -o -          # to stdout
    py-libredwg drawing.dwg --layers --problems-only
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import IO, Any

from . import __version__
from .dwg_decoder import DwgDecoder
from .dwg_parser import render_summary as _format_summary
from .errors import DwgError

__all__ = ["main", "build_parser"]

_FORMATS = ("summary", "geojson", "json")

#: Suffix -> ``--format`` mapping used when ``-o`` names a file but no
#: ``--format`` was given.  An unrecognised suffix falls back to ``summary``
#: rather than guessing: silently writing a GeoJSON blob into ``out.txt``
#: would be a surprise.
_SUFFIX_FORMATS = {
    ".geojson": "geojson",
    ".geo.json": "geojson",
    ".json": "json",
}


def build_parser() -> argparse.ArgumentParser:
    """The argument parser, built separately so tests can drive it."""
    parser = argparse.ArgumentParser(
        prog="py-libredwg",
        description=(
            "Read a DWG file (R13-R2018) in pure Python and report or export "
            "what it contains.  Read-only: this tool has no writer."
        ),
        epilog=(
            "py-libredwg is a derivative work of GNU LibreDWG and is "
            "licensed GPL-3.0-or-later.  See license_notice.md."
        ),
    )
    parser.add_argument(
        "file", metavar="FILE.dwg",
        help="the DWG file to read ('-' is not accepted for input: the "
             "format needs random access to the whole file)",
    )
    parser.add_argument(
        "-o", "--output", metavar="PATH",
        help="write the export to PATH ('-' for stdout).  Without it the "
             "result is printed on stdout.",
    )
    parser.add_argument(
        "-f", "--format", choices=_FORMATS, dest="format",
        help="output shape; inferred from the --output suffix when omitted, "
             "otherwise 'summary'",
    )
    parser.add_argument(
        "--indent", type=int, default=2, metavar="N",
        help="JSON indentation; 0 writes the compact single-line form "
             "(default: 2)",
    )
    parser.add_argument(
        "--include-erased", action="store_true",
        help="export soft-deleted entities as well as live ones.  GeoJSON "
             "omits them by default, matching dwgread",
    )
    parser.add_argument(
        "--include-proxies", action="store_true",
        help="emit a geometry-less placeholder feature for each proxy "
             "entity (in practice Tianzheng / 天正 objects, whose payload "
             "this library intentionally does not decode)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="abort on the first object that cannot be decoded instead of "
             "recording it as a problem and carrying on",
    )
    parser.add_argument(
        "--layers", action="store_true",
        help="print the layer table.  With --format summary this adds a "
             "section; with an export format it is ignored",
    )
    parser.add_argument(
        "--problems-only", action="store_true",
        help="print only the parse problems (one per line) and exit.  The "
             "exit status is still 0: problems are reported, not fatal",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="suppress the summary on stderr when writing to an output file",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"py-libredwg {__version__}",
    )
    return parser


# ---------------------------------------------------------------------------
# Summary rendering
# ---------------------------------------------------------------------------
# The census text itself is ``dwg_parser.render_summary``, imported above
# under this name.  It lives there rather than here because a library
# caller asking ``parser(path, "summary")`` must get the same numbers in
# the same order as this command line prints; keeping two copies of a
# sixty-line report is keeping two chances for them to disagree.


def _render(decoder: DwgDecoder, fmt: str, args: argparse.Namespace) -> Any:
    """Produce the export payload for ``fmt``.

    Returns a string for ``summary`` and a JSON-ready object for the other two;
    the caller decides whether to dump it to a stream or to a file, which is
    what keeps the two exporters' own file writers available to library users
    without this module duplicating their UTF-8 handling.
    """
    if fmt == "summary":
        return _format_summary(decoder, show_layers=args.layers)
    if fmt == "geojson":
        from .exporters import to_geojson

        return to_geojson(
            decoder,
            include_erased=args.include_erased,
            include_proxies=args.include_proxies,
        )
    if fmt == "json":
        from .exporters import to_json_dict

        return to_json_dict(decoder, include_erased=args.include_erased)
    raise AssertionError(f"unhandled format {fmt!r}")     # pragma: no cover


def _dump_json(payload: Any, fh: IO[str], indent: int) -> None:
    """Write JSON as UTF-8 text with no ``\\uXXXX`` escaping.

    Chinese layer, block and class names are the norm in the drawings this is
    used on; escaping them would only make the output unreadable.
    """
    json.dump(payload, fh, indent=indent or None, ensure_ascii=False)
    fh.write("\n")


def _resolve_format(args: argparse.Namespace) -> str:
    """Pick the output format from ``--format``, else the ``-o`` suffix."""
    if args.format:
        return args.format
    if args.output and args.output != "-":
        lowered = args.output.lower()
        for suffix, fmt in _SUFFIX_FORMATS.items():
            if lowered.endswith(suffix):
                return fmt
    return "summary"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _force_utf8_output() -> None:
    """Make stdout/stderr emit UTF-8 instead of the locale encoding.

    On Windows the console defaults to a legacy code page (GBK on a Chinese
    install, cp1252 elsewhere), and a drawing's layer or block names routinely
    contain characters it cannot represent — the summary then dies with a
    ``UnicodeEncodeError`` *after* a successful parse, which is the worst
    moment to lose the output.  ``errors="replace"`` is the backstop: a name
    that still cannot survive comes out with ``?`` in it rather than taking
    the whole report with it.

    No-op when the streams have already been replaced (a caller passing a
    ``StringIO``, or a non-tty pipe), which is why it is wrapped in try/except
    rather than guarded by a capability test.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):           # pragma: no cover
            pass


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line.  Returns the process exit status."""
    _force_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        decoder = DwgDecoder(args.file, strict=args.strict).parse()
    except FileNotFoundError as exc:
        print(f"py-libredwg: {exc}", file=sys.stderr)
        return 2
    except DwgError as exc:
        # Expected failure mode: a truncated file, an unsupported version, a
        # bit stream that ran off the end.  Reported as a plain message, not a
        # traceback — the traceback would name decoder internals a user cannot
        # act on.
        print(f"py-libredwg: cannot decode {args.file}: {exc}", file=sys.stderr)
        return 1

    if args.problems_only:
        for problem in decoder.problems:
            print(problem)
        return 0

    fmt = _resolve_format(args)
    payload = _render(decoder, fmt, args)

    if args.output and args.output != "-":
        try:
            with open(args.output, "w", encoding="utf-8") as fh:
                if fmt == "summary":
                    fh.write(payload + "\n")
                else:
                    _dump_json(payload, fh, args.indent)
        except OSError as exc:
            print(f"py-libredwg: cannot write {args.output}: {exc}",
                  file=sys.stderr)
            return 1
        if not args.quiet:
            print(f"py-libredwg: wrote {args.output} ({fmt})", file=sys.stderr)
        return 0

    if fmt == "summary":
        print(payload)
    else:
        _dump_json(payload, sys.stdout, args.indent)
    return 0


if __name__ == "__main__":                                # pragma: no cover
    raise SystemExit(main())
