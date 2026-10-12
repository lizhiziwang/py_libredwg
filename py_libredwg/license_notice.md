# License Notice — py-libredwg

## SPDX-License-Identifier: GPL-3.0-or-later

**py-libredwg is a derivative work of GNU LibreDWG.**

This project is a Python translation and re-implementation of parts of the
GNU LibreDWG library. It is **not** an independent clean-room implementation.
The parsing logic, bit-stream semantics, section layouts, object type codes and
field ordering implemented here were translated from the LibreDWG C sources.

---

## 1. Upstream work

| | |
|---|---|
| **Project** | GNU LibreDWG |
| **Upstream URL** | https://github.com/LibreDWG/libredwg |
| **Upstream home** | https://www.gnu.org/software/libredwg/ |
| **Copyright** | Copyright (C) 2009, 2010, 2015, 2018–2026 Free Software Foundation, Inc. |
| **Upstream license** | GNU General Public License, version 3 **or (at your option) any later version** |
| **Reference revision** | `a3d51f13f7afc143c2adb87d4b0e858e24cdac95` (master, 2026-10-02) |

Upstream files consulted during translation:

```
src/bits.c            bit-stream reader primitives
src/bits.h            Bit_Chain, bit-order rules
src/decode.c          header / section / object decode
src/dwg.c             file open, version detection, section location
src/decode_r2007.c    R2004+ second header, section maps, page decompression
src/reedsolomon.c     Reed-Solomon (255,239) decode for the section map
src/objects.c         HANDLES / object-map section
src/classes.c         CLASSES section
src/dwg.spec          object & entity field specifications
src/common_entity_data.spec
src/common_entity_handle_data.spec
src/2ndheader.spec    R2004+ second header layout
src/r2004_file_header.spec
src/header_variables.spec
src/out_geojson.c     reference behaviour for GeoJSON export
src/out_json.c        reference behaviour for JSON export
```

## 2. License of this work

Because this project is derived from LibreDWG, it **must** be distributed under
the GNU General Public License, and it **must not** be relicensed under a
permissive license such as MIT, BSD or Apache-2.0.

This project is distributed as **GPL-3.0-or-later**.

> **Note on the choice of `-or-later` rather than `-only`:**
> LibreDWG is licensed "version 3 of the License, or (at your option) any later
> version". A derivative work receives that grant and therefore cannot narrow
> it. Declaring this project `GPL-3.0-only` would restrict a right the upstream
> authors granted to downstream recipients. `GPL-3.0-or-later` is the correct
> and safe identifier for a derivative of `GPL-3.0-or-later` code.

You should have received a copy of the GNU General Public License along with
this program. If not, see <https://www.gnu.org/licenses/>.

## 3. Obligations if you redistribute this

1. Keep this notice and the upstream copyright notice intact.
2. Ship the full GPLv3 text (or a link to it) with any distribution.
3. Provide the complete corresponding source of your modified version.
4. State prominently that your version is modified and give the modification date.
5. Do not add any further restriction on top of the GPLv3 terms.

## 4. Test-data attribution

The sample `.dwg` files under `tests/data/` are copied from the LibreDWG test
suite (`test/test-data/`) and remain under the upstream GPL-3.0-or-later terms.
The accompanying `.txt` files are entity property listings used as comparison
fixtures. Both are redistributed here solely as test fixtures.

DWG is a proprietary file format created by Autodesk, Inc. "AutoCAD" and "DWG"
are trademarks of Autodesk, Inc. This project is not affiliated with, endorsed
by, or sponsored by Autodesk. It reads files produced by Autodesk software
purely for interoperability, which is the same purpose and legal basis as the
upstream GNU LibreDWG project.

## 5. No warranty

This program is distributed in the hope that it will be useful, but **WITHOUT
ANY WARRANTY**; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
