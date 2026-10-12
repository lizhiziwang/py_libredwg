#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""Open a DWG, parse it, export GeoJSON — the whole MVP in one file.

Run it on a drawing of your own::

    python examples/read_and_export.py drawing.dwg
    python examples/read_and_export.py drawing.dwg out/

It prints a census, then writes ``<stem>.geojson`` and ``<stem>.json`` next to
the input (or into the directory you name).  Every step below is a public API
call; nothing here reaches into decoder internals, so the same four calls work
from a script of your own.

To compare against ground truth, run LibreDWG's own reader over the same file
and diff::

    dwgread -O geojson -o mine.geojson  drawing.dwg    # this library
    dwgread -O geojson -o theirs.geojson drawing.dwg   # upstream C

Entity handles, layer names and coordinates should agree.  See the README's
"Verifying a parse" section for what to look at when they do not.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from py_libredwg import (
    Arc,
    Circle,
    DwgDecoder,
    Line,
    LWPolyline,
    Point,
)
from py_libredwg.exporters import export_geojson, export_json
from py_libredwg.geometry import Vec3


def force_utf8_output() -> None:
    """Print UTF-8 on a console that does not default to it.

    A Windows console runs on a legacy code page (GBK on a Chinese install),
    and the layer names in a Chinese drawing cannot all be encoded in it —
    without this the report dies with a ``UnicodeEncodeError`` *after* a
    successful parse.  ``errors="replace"`` is the backstop.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def census(decoder: DwgDecoder) -> None:
    """Print what the file turned out to contain."""
    print(f"{decoder.source}")
    print(f"  version   {decoder.version_tag} ({decoder.version.name})")
    print(f"  objects   {len(decoder.objects)}")
    print(f"  entities  {len(decoder.entities)}")
    print(f"  layers    {len(decoder.layers)}")
    print(f"  blocks    {len(decoder.blocks)}")

    counts = Counter(obj.type_name for obj in decoder.entities)
    if counts:
        print("  by type")
        for name, count in counts.most_common():
            print(f"    {name:<12} {count:>6}")

    # Proxy entities are the ones this library refuses to look inside.  They
    # are usually Tianzheng (天正) objects, whose meaning lives only in the
    # plugin that wrote them; LibreDWG does not decode them either.
    if decoder.proxies:
        print(f"  proxies   {len(decoder.proxies)} (payload preserved, "
              f"not decoded)")

    # A problem is not an exception — the decoder records what it could not
    # make sense of and keeps going, exactly as dwgread does.
    if decoder.problems:
        print(f"  problems  {len(decoder.problems)}")
        for problem in decoder.problems[:10]:
            print(f"    - {problem}")
        if len(decoder.problems) > 10:
            print(f"    ... {len(decoder.problems) - 10} more")
    else:
        print("  problems  none")


def layer_table(decoder: DwgDecoder) -> None:
    """Print the layer table with each layer's resolved colour.

    Entities set to *ByLayer* carry no colour of their own; this is where that
    colour actually lives.
    """
    if not decoder.layers:
        return
    print("  layers")
    for layer in decoder.layers:
        flags = [
            name for name, on in (
                ("off", layer.is_off),
                ("frozen", layer.is_frozen),
                ("locked", layer.is_locked),
            ) if on
        ]
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        print(f"    0x{layer.handle:X}  {layer.name}  "
              f"aci={layer.color.index}{suffix}")


def entity_sample(decoder: DwgDecoder, limit: int = 5) -> None:
    """Print a few typed entities in full.

    Only the five MVP types carry geometry, and only they get a class of their
    own — every other object is retained with its raw bytes and its type name,
    so ``isinstance`` against the typed classes is the way to tell them apart.
    """
    typed = [
        e for e in decoder.entities
        if isinstance(e, (Line, Circle, Arc, Point, LWPolyline))
    ]
    if not typed:
        return
    print(f"  first {min(limit, len(typed))} typed entities")
    for entity in typed[:limit]:
        print(f"    0x{entity.handle:X} {entity.type_name} "
              f"layer={entity.layer!r} {entity.geometry_type} "
              f"{_describe(entity)}")


def _describe(entity: object) -> str:
    """A one-line geometry summary, per entity class."""
    if isinstance(entity, Line):
        return f"{_pt(entity.start)} -> {_pt(entity.end)}"
    if isinstance(entity, Circle):
        return f"center {_pt(entity.center)} r={entity.radius:g}"
    if isinstance(entity, Arc):
        # Stored in radians, as the DWG does; DXF group 50/51 would be
        # degrees, which is the one place the two formats disagree.
        return (f"center {_pt(entity.center)} r={entity.radius:g} "
                f"angles {entity.start_angle:.4f}..{entity.end_angle:.4f} rad")
    if isinstance(entity, Point):
        return _pt(entity.location)
    if isinstance(entity, LWPolyline):
        closed = "closed" if entity.closed else "open"
        return f"{len(entity.vertices)} vertices, {closed}"
    return ""                                              # pragma: no cover


def _pt(v: Vec3) -> str:
    return f"({v.x:g}, {v.y:g}, {v.z:g})"


def main(argv: list[str]) -> int:
    force_utf8_output()
    if not 1 <= len(argv) <= 2:
        print(__doc__.splitlines()[0], file=sys.stderr)
        print(f"usage: {Path(sys.argv[0]).name} FILE.dwg [OUTDIR]",
              file=sys.stderr)
        return 2

    source = Path(argv[0])
    outdir = Path(argv[1]) if len(argv) == 2 else source.parent
    outdir.mkdir(parents=True, exist_ok=True)

    # 1. Open and parse.  ``strict=False`` (the default) mirrors dwgread: a
    #    single unreadable object does not cost you the rest of the drawing.
    decoder = DwgDecoder(source).parse()

    # 2. Report.  Iterating ``.parse()`` twice is a no-op, so calling the
    #    helpers below costs nothing but the printing.
    census(decoder)
    layer_table(decoder)
    entity_sample(decoder)
    print()

    # 3. GeoJSON — one Feature per entity, geometry mapped the way LibreDWG's
    #    out_geojson.c maps it, so the file can be diffed against dwgread.
    #    Erased entities and proxies are skipped by default.
    geojson_path = outdir / (source.stem + ".geojson")
    count = export_geojson(decoder, geojson_path)
    print(f"wrote {geojson_path}  ({count} features)")

    # 4. Plain JSON — the same parse without a geometry standard imposed on
    #    it.  This is the one to look at when a parse seems wrong: it carries
    #    the header variables, the class table, per-entity fields and a census
    #    of everything that was not decoded.
    json_path = outdir / (source.stem + ".json")
    count = export_json(decoder, json_path)
    print(f"wrote {json_path}  ({count} entities)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
