# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""One-call front end: a path and a target format in, the result back.

::

    from py_libredwg.dwg_parser import parser

    geo = parser("drawing.dwg", "geojson")   # a FeatureCollection dict
    doc = parser("drawing.dwg", "json")      # the structured export
    print(parser("drawing.dwg", "summary"))  # the human-readable census

    parser("drawing.dwg", "geojson", out="drawing.geojson")   # also write it
    parser("drawing.dwg", "json", out_dir="build/")   # -> build/drawing.json

:func:`parser` is a thin composition of
:class:`~py_libredwg.dwg_decoder.DwgDecoder` and :mod:`py_libredwg.exporters`.
It holds no decoding logic of its own — reach for it when a script wants "this
drawing as X" in one line, and for :class:`DwgDecoder` directly when the object
model itself is what you need (entities, layers, handles, per-object geometry).

The format vocabulary is the same three the command line uses — ``summary``,
``geojson``, ``json`` — and :func:`render_summary` is where the command line's
census text comes from, so the library and the ``py-libredwg`` script cannot
drift apart in what they report.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .codepages import codepage_to_python
from .dwg_decoder import DwgDecoder
from .exporters import to_geojson, to_json_dict

__all__ = ["FORMATS", "parser", "render", "render_summary"]

#: The output shapes :func:`parser` understands.
FORMATS = ("summary", "geojson", "json")

#: Lower-cased spellings that resolve onto :data:`FORMATS`.  The short forms and
#: the case-insensitive match are here because ``"geo"`` and ``"GeoJSON"`` are
#: both things a caller will type.  A format that is *not* in this table is a
#: mistake, and raising beats silently handing back one of the others.
_ALIASES = {
    "summary": "summary",
    "text": "summary",
    "geojson": "geojson",
    "geo": "geojson",
    "json": "json",
    "dict": "json",
}

#: Extension per format, for the ``out_dir`` form of ``out``.  The three are
#: distinct so that writing two formats into one directory cannot land them on
#: the same filename and silently overwrite the first with the second.
_EXTENSIONS = {"summary": ".txt", "geojson": ".geojson", "json": ".json"}


def _normalise(target_format: str) -> str:
    """Map a caller's spelling onto one of :data:`FORMATS`.

    Raises
    ------
    ValueError
        If the format is not one this module can produce.  Deliberately not a
        :class:`~py_libredwg.errors.DwgError`: a bad format argument is a bug in
        the *caller*, not a fault in the drawing, and the two should not be
        catchable by the same handler.
    """
    key = target_format.strip().lower()
    try:
        return _ALIASES[key]
    except KeyError:
        raise ValueError(
            f"unknown target format {target_format!r}; expected one of "
            f"{', '.join(FORMATS)}"
        ) from None


# ---------------------------------------------------------------------------
# The summary text
# ---------------------------------------------------------------------------
def render_summary(decoder: DwgDecoder, *, show_layers: bool = False) -> str:
    """The human-readable census of a decoded drawing.

    Ordered so the first few lines answer the questions one actually has about
    a drawing — what version is this, how much is in it, did anything go wrong
    — before the detail.  This is the text ``py-libredwg drawing.dwg`` prints;
    it lives here rather than in :mod:`py_libredwg.cli` so that a library caller
    asking for ``"summary"`` gets byte-identical output to the command line.
    """
    header = decoder.header
    lines: list[str] = []

    lines.append(f"file        : {decoder.source}")
    lines.append(f"version     : {decoder.version_tag} ({decoder.version.name})")
    if header is not None:
        # ``FileHeader.codepage`` is the number from the first header block;
        # showing the codec it resolves to saves a lookup for the one thing
        # anybody does with this number — decode the text.
        codec = codepage_to_python(header.codepage)
        lines.append(f"code page   : {header.codepage} ({codec})")
        lines.append(f"maint ver   : {header.maint_version}")

    lines.append("")
    lines.append(f"objects     : {len(decoder.objects)}")
    lines.append(f"entities    : {len(decoder.entities)}")
    lines.append(f"geometry    : {len(decoder.geometry_entities)}")
    lines.append(f"classes     : {len(decoder.classes)}")
    lines.append(f"layers      : {len(decoder.layers)}")
    lines.append(f"blocks      : {len(decoder.blocks)}")
    lines.append(f"proxies     : {len(decoder.proxies)}")
    lines.append(f"undecoded   : {len(decoder.unknown)}")

    # Entity census by type — the quickest way to see whether this drawing is
    # made of the five types the MVP understands or something else entirely.
    counts = Counter(obj.type_name for obj in decoder.entities)
    if counts:
        lines.append("")
        lines.append("entities by type")
        width = max(len(name) for name in counts)
        for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {name:<{width}}  {count}")

    if show_layers:
        lines.append("")
        lines.append("layers")
        for layer in decoder.layers:
            flags = []
            if layer.is_off:
                flags.append("off")
            if layer.is_frozen:
                flags.append("frozen")
            if layer.is_locked:
                flags.append("locked")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(
                f"  0x{layer.handle:X}  {layer.name}  "
                f"color={layer.color.index}{suffix}"
            )

    if decoder.problems:
        lines.append("")
        lines.append(f"problems    : {len(decoder.problems)}")
        for problem in decoder.problems:
            lines.append(f"  - {problem}")
    else:
        lines.append("")
        lines.append("problems    : none")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rendering an already-decoded drawing
# ---------------------------------------------------------------------------
def render(
    decoder: DwgDecoder,
    target_format: str,
    *,
    include_erased: bool = False,
    include_proxies: bool = False,
    show_layers: bool = False,
) -> Any:
    """Render a decoded drawing in ``target_format``.

    Split out from :func:`parser` so a caller who already holds a
    :class:`DwgDecoder` — perhaps decoded it once and wants two outputs, or
    inspected it before choosing — does not have to re-parse the file.

    Returns a ``str`` for ``"summary"`` and a JSON-serialisable ``dict``
    otherwise.  GeoJSON omits erased entities and proxies by default, matching
    ``dwgread``; the flags opt them back in.
    """
    fmt = _normalise(target_format)

    if fmt == "summary":
        return render_summary(decoder, show_layers=show_layers)
    if fmt == "geojson":
        return to_geojson(
            decoder,
            include_erased=include_erased,
            include_proxies=include_proxies,
        )
    # "json" — the only format left, and the one _normalise guarantees is valid.
    return to_json_dict(decoder, include_erased=include_erased)


def _write(payload: Any, out: str | Path, fmt: str) -> None:
    """Write a rendered payload to ``out`` as UTF-8.

    ``ensure_ascii=False`` because the drawings this is used on have Chinese
    layer, block and class names; escaping them to ``\\uXXXX`` would only make
    the file unreadable in the editor it is opened in next.
    """
    with open(out, "w", encoding="utf-8") as fh:
        if fmt == "summary":
            fh.write(payload + "\n")
        else:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")


# ---------------------------------------------------------------------------
# The one call
# ---------------------------------------------------------------------------
def parser(
    dwg_path: str | Path,
    target_format: str = "geojson",
    *,
    strict: bool = False,
    include_erased: bool = False,
    include_proxies: bool = False,
    out: str | Path | None = None,
    out_dir: str | Path | None = None,
) -> Any:
    """Read a DWG file and return it in ``target_format``.

    Parameters
    ----------
    dwg_path:
        Path to the ``.dwg`` file.  R13 through R2018 (``AC1015`` .. ``AC1032``)
        are supported; the version is detected from the file, not supplied.
    target_format:
        One of ``"summary"``, ``"geojson"`` or ``"json"``, matched
        case-insensitively and with ``"geo"``/``"text"``/``"dict"`` accepted as
        short forms.  See :func:`render` for what each returns.
    strict:
        ``False`` (the default, matching ``dwgread``) records an object that
        cannot be decoded as a problem and carries on.  ``True`` raises on the
        first one.
    include_erased, include_proxies:
        Exporter flags; ignored by ``target_format="summary"``.
    out:
        When given, the rendered payload is also written there as UTF-8 —
        pretty-printed for the export formats, plain text for the summary.  The
        returned value is the same either way, so a caller can write and use it
        without parsing the file twice.  Nothing is written by default: a
        parsing call should not have a side effect on disk unless asked.  An
        empty string counts as not given, like ``None``.  This must be a file
        path: a directory here is the ``out``/``out_dir`` mix-up, and is refused
        with a ``ValueError`` rather than left to surface as a permission error
        from ``open``.
    out_dir:
        The directory form of ``out``.  The file is named after the *drawing*
        rather than after the format — ``out_dir="build"`` with
        ``dwg_path="plan.dwg"`` writes ``build/plan.geojson`` (or ``plan.json``,
        or ``plan.txt`` for the summary) — so a batch keeps the identity of each
        input.  The directory is created if it does not exist, parents included.
        Mutually exclusive with ``out``.

    Returns
    -------
    str
        For ``"summary"`` — the census text.
    dict
        For ``"geojson"`` and ``"json"`` — the export payload, ready for
        ``json.dump``.

    Raises
    ------
    FileNotFoundError
        If ``dwg_path`` does not exist.
    ValueError
        If ``target_format`` is not a format this module can produce, if both
        ``out`` and ``out_dir`` are given, or if ``out`` names a directory.
    py_libredwg.errors.DwgError
        If the file cannot be decoded — a corrupt or truncated file, an
        unsupported version, or a bit stream that ran off the end.  The
        subclasses (:class:`~py_libredwg.errors.DwgDecodeError`,
        :class:`~py_libredwg.errors.BitStreamError`, ...) say which.

    Examples
    --------
    ::

        from py_libredwg.dwg_parser import parser

        feats = parser("drawing.dwg", "geojson")["features"]
        print(len(feats), "features")

        parser("drawing.dwg", "json", out="drawing.json")
        parser("drawing.dwg", "geojson", out_dir="build")   # build/drawing.geojson
    """
    fmt = _normalise(target_format)

    if out and out_dir:
        # Two ways of saying "write it", and they disagree about the filename.
        # Refusing beats picking one: whichever lost, the caller would go
        # looking for a file that was never written.  An empty string is not a
        # location, so `out=""` is read as "not given" rather than as a write,
        # and in particular does not collide with a real out_dir.
        raise ValueError(
            "pass either out= (an exact file) or out_dir= (a directory, with "
            "the name derived from the drawing), not both"
        )

    if out and Path(out).is_dir():
        # The out=/out_dir= mix-up, reported in the caller's terms.  Left to
        # open() this is a bare PermissionError on Windows and an
        # IsADirectoryError elsewhere; both name the path but not the mistake,
        # and neither points at the parameter that would have worked.
        raise ValueError(
            f"out={str(out)!r} is a directory, not a file; pass out_dir= to "
            f"write a file named after the drawing, or give out= a full path "
            f"including the filename"
        )

    decoder = DwgDecoder(dwg_path, strict=strict).parse()
    payload = render(
        decoder,
        fmt,
        include_erased=include_erased,
        include_proxies=include_proxies,
    )

    if out_dir:
        directory = Path(out_dir)
        # Created rather than demanded: naming a directory is the caller saying
        # where the output belongs, and pre-creating it is busywork.  parents=
        # so `out_dir="build/exports"` works on a cold tree.
        directory.mkdir(parents=True, exist_ok=True)
        out = directory / (Path(dwg_path).stem + _EXTENSIONS[fmt])

    if out:
        _write(payload, out, fmt)

    return payload
