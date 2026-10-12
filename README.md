# py-libredwg

**Read DWG files in pure Python.** No C extension, no SWIG, no `ctypes`, no
external process — the decoder is a translation of GNU LibreDWG's C logic into
Python.

> ## ⚠️ License: GPL-3.0-or-later — not MIT
>
> **This project is a derivative work of [GNU LibreDWG](https://github.com/LibreDWG/libredwg).**
> It is *not* an independent clean-room implementation of the DWG format: the
> bit-stream semantics, section layouts, object type codes and field ordering
> implemented here were translated from LibreDWG's C sources. LibreDWG is
> licensed GPL-3.0-or-later, so this work is necessarily licensed the same way.
>
> **Do not relicense it as MIT, BSD or Apache.** If you need permissive terms
> for DWG reading, use a different library; there is no clean-room path from
> this codebase to one. See [`py_libredwg/license_notice.md`](py_libredwg/license_notice.md)
> for the full statement and [`LICENSE`](LICENSE) for the license text.

---

## Status: MVP prototype — read this before relying on it

This is a **prototype**. It parses the fixture drawings in `tests/data/`
exactly, and it parses a few real survey drawings cleanly, but it has **not
been validated against large-scale production drawings**. Large AutoCAD
drawings exercise entity types, proxy objects, unusual code pages and
malformed-but-tolerated records that no small fixture does, and there are
certainly paths through the format this implementation gets wrong.

Treat a successful parse as *evidence*, not proof. Validate against your own
data before depending on the output. Bug reports with a sample file are the
most useful thing you can send.

### What works

Read-only parsing of **R13 through R2018** (`AC1015` … `AC1032`):

| Section | Status |
|---|---|
| HEADER (file header + header variables) | decoded |
| CLASSES | decoded |
| TABLES | LAYER, LTYPE, STYLE, … — names, handles, flags, colours |
| BLOCKS | block headers and their names; entities carry owner handles |
| ENTITIES | LINE, LWPOLYLINE, CIRCLE, ARC, POINT decoded to typed objects |
| ProxyEntity / unknown types | raw bytes preserved, **body not decoded** |
| OBJECTS map, handle stream, CRC-16 checks | decoded and verified |

Five entity types are decoded in full. Everything else is retained as an
object with its type code, type name and raw bytes, so a caller can still
census and count what was not understood — which for a Chinese drawing is
usually most of the file. **Undecoded objects usually report handle 0**, and
that is deliberate rather than a gap: their handle sits at the head of the
handle stream, whose position depends on where their value stream ends, which
is precisely what is unknown for a type we cannot lay out. Upstream has the
same hole (``UNKNOWN_ENT``/``UNKNOWN_OBJ`` in ``dwg2.spec`` skip the common
handle data, and ``dwg_decode_add_object`` comments its handle-map insert
"empty only with UNKNOWN"). Guessing a position would attach a plausible wrong
number to every undecoded object, so it is left zero.

### Known limitations

- **ProxyEntity is never decoded.** In practice these are Tianzheng (天正)
  custom objects, and their meaning lives only in the plugin that wrote them
  — there is no public specification to decode against. LibreDWG does not
  decode them either. The raw payload is preserved byte-for-byte so you can
  hand it to something that can; the exporters report their presence and size
  and nothing more.
- **Read-only. There is no writer.** This library cannot modify, re-save or
  convert to DWG. Nothing in the design anticipates one.
- **Block nesting is not expanded.** `INSERT` entities are not decoded, so a
  block's contents are reachable only through the block definition and its
  owner handles, not by walking a placement tree. Entity geometry is the
  model-space / paper-space geometry; block-relative geometry is not
  transformed to world coordinates.
- **The MVP entity set is five types.** TEXT, MTEXT, INSERT, HATCH, SPLINE,
  ELLIPSE, DIMENSION, 3D entities and everything else are undecoded objects
  with preserved bytes. See [Roadmap](#roadmap).
- **No DXF export.** GeoJSON and a plain JSON view only. (LibreDWG itself
  writes DXF; this does not, yet.)
- **Memory is not optimised for very large files.** The file image is read
  once and shared, and the object map is scanned in place, but every decoded
  object is held in memory at once. A drawing with several hundred thousand
  entities will use several hundred megabytes.
- **Cosmetic discrepancy, investigated and attributed to the fixtures**: for
  `$EXTMIN.z` the R2000 and R2018 `example_*.dwg` files disagree with the
  `.dxf` files shipped beside them upstream, while R2004–R2013 agree exactly
  and x/y agree for every version. A full-file scan finds the `.dxf`'s value
  nowhere in the `.dwg`, and the `.dwg` files are byte-identical (md5) to
  upstream's copies — so the checked-in `.dxf` references look stale rather
  than the decode being wrong. Recorded here rather than silently fixed,
  because it is unresolved.

---

## Install

```bash
pip install .            # numpy is the only runtime dependency
pip install ".[test]"    # + pytest
```

Python 3.10 or newer. `numpy` is used for the bit-level work; nothing else is
required at runtime.

## Quick start

```python
from py_libredwg import DwgDecoder
from py_libredwg.exporters import export_geojson, export_json

doc = DwgDecoder("drawing.dwg").parse()

print(doc.version_tag)          # 'AC1032'
print(len(doc.entities))        # 1573
print(doc.entities[0].layer)    # 'WALLS'
print(doc.problems)             # [] when nothing was odd

export_geojson(doc, "drawing.geojson")
export_json(doc, "drawing.json")
```

If you only want the result and not the object model, `dwg_parser.parser` is the
one-call form — a path and a target format:

```python
from py_libredwg.dwg_parser import parser

print(parser("drawing.dwg", "summary"))          # the census text
features = parser("drawing.dwg", "geojson")["features"]
parser("drawing.dwg", "json", out="drawing.json")   # also write it
parser("drawing.dwg", "geojson", out_dir="build")   # -> build/drawing.geojson
```

The format is the same `summary` / `geojson` / `json` the command line takes
(`geo`, `text` and `dict` are accepted as short forms, matched
case-insensitively); an unrecognised one raises `ValueError` rather than
silently picking a default. Nothing is written to disk unless `out=` or
`out_dir=` is given — `out_dir` names the file after the *drawing*
(`out_dir="build"` on `plan.dwg` writes `build/plan.geojson`, `build/plan.json`
or `build/plan.txt`), creating the directory if needed, so a batch keeps track
of which output came from which input.

There is a runnable version of this in
[`examples/read_and_export.py`](examples/read_and_export.py):

```bash
python examples/read_and_export.py drawing.dwg out/
```

## Command line

Installed as `py-libredwg`:

```bash
py-libredwg drawing.dwg                      # a human-readable census
py-libredwg drawing.dwg --layers             # ... plus the layer table
py-libredwg drawing.dwg -o drawing.geojson   # format from the suffix
py-libredwg drawing.dwg --format json -o -   # JSON on stdout
py-libredwg drawing.dwg --problems-only      # just the parse problems
```

`--include-erased` adds soft-deleted entities (GeoJSON omits them, matching
`dwgread`). `--include-proxies` emits a geometry-less placeholder feature per
proxy so you get a census of them in the output.

Exit status is `0` on a successful parse even when problems were recorded —
problems are reported, not fatal — `1` when the file could not be decoded,
and `2` for a missing file or a usage error.

## What a decoded object looks like

```python
>>> from py_libredwg import DwgDecoder
>>> doc = DwgDecoder("tests/data/2018/Line.dwg").parse()
>>> doc.entities[0].start
Vec3(x=21.607766723020987, y=2.811923503435004, z=0.0)
>>> doc.entities[0].layer, hex(doc.entities[0].handle)
('0', '0x1ca')
>>> doc.problems
[]
```

(The `repr` of a `Line` is the full dataclass — forty-odd fields including
the common entity data, the colour and the extrusion. `start` and `end` above
are the two that matter for a line.)

Objects are `@dataclass(slots=True)` instances. There are no C pointers, no
manual allocation and no caller-owned buffers to free: the C structs became
dataclasses, `malloc`/`free` disappeared into Python's memory management, and
the C error-code returns became exceptions (`DwgError` and its subclasses —
`DwgDecodeError`, `DwgFormatError`, `DwgCRCError`, `DwgSectionError`,
`BitStreamError`, `UnsupportedVersionError`, `UnsupportedObjectError`).

`DwgDecoder(strict=False)` — the default — mirrors `dwgread`: an object that
cannot be decoded becomes an `UnknownObject` with its raw bytes and an entry
in `decoder.problems`, rather than taking the whole drawing down with it. Pass
`strict=True` to make the first failure raise instead.

---

## Verifying a parse

**Use LibreDWG's own `dwgread` as ground truth.** It is the reference
implementation, and comparing against it is the only way to tell a
misunderstanding of the format from a quirk of your file:

```bash
# upstream C, as the reference
dwgread --json drawing.dwg > theirs.json
dwgread -O geojson -o theirs.geojson drawing.dwg

# this library
py-libredwg drawing.dwg --format json -o mine.json
py-libredwg drawing.dwg -o mine.geojson
```

Then compare in that order — each step narrows the search:

1. **Entity count.** A differing count means a decode failure or a
   disagreement about what counts as an entity. Check `decoder.problems`
   first; a recorded problem usually names the object.
2. **Handles.** Each entity's handle must match one-for-one. Handles are the
   file's own addressing, so a mismatch means the object map or the object
   header was read wrong.
3. **Layer names.** Grader still: the LAYER table decode and the per-entity
   layer reference are separate code paths, so a layer mismatch but matching
   handles points at the table.
4. **Coordinates last.** Geometry differences are usually a downstream
   consequence of one of the three above — or an OCS/extrusion transform
   not applied, or a bulge not tessellated. Compare a *single* entity
   field-by-field before drawing conclusions about the whole drawing.

The suite in [`tests/test_dwg_decode.py`](tests/test_dwg_decode.py) does this
with AutoCAD VLA property dumps instead of `dwgread`, because those need no C
build: for each of the five MVP entity types, in each of the six versions,
handles, layer, coordinates, radii, angles and lineweights are compared
against the vendor's own reading of the file.

```bash
python -m pytest tests/test_dwg_decode.py -q
```

(`python -m pytest` puts the repository root on `sys.path` by itself, so no
`PYTHONPATH` is needed. A bare `pytest` would not — `pyproject.toml` sets
`pythonpath = ["."]` to cover that case too.)

If you have `dwgread` available, adding its output as a second ground truth is
the single most valuable contribution to this project.

---

## Module map

| Module | Translation of |
|---|---|
| `bitreader.py` | `src/bit.c` — bit-stream primitives, `BS`/`BL`/`BD`/`MS`/`H`/`CMC`/`ENC`/`TV`/`TU` |
| `dwg_decoder.py` | `src/dwg.c`, `src/decode.c` — open the file, detect the version, walk the main flow |
| `header.py` | `src/decode.c`, `*.spec` — file header, R2004/R2007 headers, header variables, CRC-16 |
| `sections.py` | `src/decode.c`, `src/decode_r2007.c`, `src/dwg_objects.c`, `src/dwg.spec` — sections, object containers, the object decoder |
| `objects/` | one dataclass per DWG type (`LINE`, `LWPOLYLINE`, `CIRCLE`, `ARC`, `POINT`, `LAYER`, `BLOCK_HEADER`, `ProxyEntity`, …) |
| `exporters/` | `src/out_geojson.c`, and a plain JSON view |
| `dwg_parser.py` | the one-call front end — `parser(path, format)`, and the census text the CLI prints |
| `constants.py` | `src/dwg.h`, `src/dwg.c` — versions, type codes, the ACI palette, lineweights |
| `codepages.py` | `src/codepages.h` |
| `errors.py` | the `DWG_ERR_*` return codes, as exceptions |

### A note on the bit reader

The bit reader operates on one immutable `bytes` object for the whole file —
loaded once, never copied per read. `numpy` is used for the bit-granular
operations, and **no array is allocated per bit read**: the reader keeps
`byte` and `bit` as its position (byte-level fields are peeked through
slicing and `struct.unpack_from`, both of which are views), so reading a field
costs a shift and a mask, not an allocation. Backward seeking is not
supported, because no decoder path needs it.

---

## Roadmap

Not in this MVP, in rough order of value:

1. More entity types — TEXT/MTEXT first, then INSERT (which is what makes
   block nesting walkable), then HATCH, SPLINE, ELLIPSE.
2. Ground-truth comparison against `dwgread` output built into the test suite.
3. DXF export, so output can be fed to existing CAD tooling.
4. Lower memory use on large drawings — stream objects instead of holding
   them all, and drop raw bytes once an object is decoded.
5. ProxyEntity internals — still out of scope, and likely to stay there
   without a specification.

## Contributing

Reports of files that parse wrongly are welcome, and a file that parses
cleanly but disagrees with `dwgread` is more valuable still. Please include
the DWG version and, if you can, the `dwgread` output for the same file.

## License

**GPL-3.0-or-later**, as a derivative work of GNU LibreDWG. Copyright (C)
2009, 2010, 2015, 2018–2026 Free Software Foundation, Inc. for the upstream
portions; see [`py_libredwg/license_notice.md`](py_libredwg/license_notice.md)
and [`LICENSE`](LICENSE).
