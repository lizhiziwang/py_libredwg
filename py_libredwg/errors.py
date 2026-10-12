# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# This file is part of a derivative work of GNU LibreDWG
# (https://github.com/LibreDWG/libredwg), Copyright (C) 2009, 2010, 2015,
# 2018-2026 Free Software Foundation, Inc.
# Licensed under the GNU General Public License v3 or later.
# See py_libredwg/license_notice.md for details.
"""Exception hierarchy for :mod:`py_libredwg`.

LibreDWG signals almost every failure by returning a negative ``DWG_ERR_*``
code from a ``dwg_decode_*`` function and setting ``dat->byte`` /
``dat->bit`` to a sentinel.  In Python that style is replaced by exceptions:
every failure path raises a subclass of :class:`DwgDecodeError`.

The mapping is deliberate and one-way — a raised exception means the parse
*cannot* continue, exactly as a non-``DWG_ERR_NONE`` return in the C code
means the caller should unwind.

Hierarchy::

    DwgError
     └── DwgDecodeError            (raised for any malformed input)
          ├── DwgFormatError       (truncated / corrupt container)
          ├── UnsupportedVersionError
          ├── BitStreamError
          ├── DwgSectionError
          ├── UnsupportedObjectError
          └── DwgCRCError
"""

from __future__ import annotations

__all__ = [
    "DwgError",
    "DwgDecodeError",
    "DwgFormatError",
    "DwgCRCError",
    "DwgSectionError",
    "BitStreamError",
    "UnsupportedVersionError",
    "UnsupportedObjectError",
]


class DwgError(Exception):
    """Base class for every error raised by py-libredwg.

    Catching this catches everything the library raises.
    """


class DwgDecodeError(DwgError):
    """A DWG file could not be decoded.

    This is the general failure class, raised for damaged files, impossible
    field values and any other condition that makes the parse unrecoverable.
    Subclasses narrow the cause; code that just wants "it did not parse"
    should catch this class.
    """

    def __init__(self, message: str, *, offset: int | None = None,
                 section: str | None = None) -> None:
        self.offset = offset
        self.section = section
        parts = [message]
        if section is not None:
            parts.append(f"section={section}")
        if offset is not None:
            parts.append(f"offset=0x{offset:x}")
        super().__init__(" | ".join(parts))


class DwgFormatError(DwgDecodeError):
    """The container is malformed or truncated.

    Equivalent to running off the end of ``dat->chain`` in the C code, or to
    a section whose declared size runs past end-of-file.
    """


class DwgCRCError(DwgDecodeError):
    """A CRC / checksum check failed.

    LibreDWG verifies CRCs on several structures (the R2004+ section page
    header and the page data).  A mismatch is reported rather than silently
    tolerated, because a bad CRC almost always means the whole section is
    garbage.
    """


class DwgSectionError(DwgDecodeError):
    """A required section is missing, empty or unreadable.

    ``section`` carries the section name (``"HEADER"``, ``"OBJECTS"``, ...).
    """


class BitStreamError(DwgDecodeError):
    """A bit-stream read went past the end of its buffer, or produced a
    structurally impossible value (e.g. a negative string length).

    Mirrors the ``DWG_ERR_*`` codes returned by ``bits.c`` helpers.
    """

    def __init__(self, message: str, *, offset: int | None = None,
                 section: str | None = None) -> None:
        super().__init__(message, offset=offset, section=section)


class UnsupportedVersionError(DwgDecodeError):
    """The DWG version is not one this library can read.

    Raised when the 6-byte version tag at offset 0 is neither a known
    ``AC10xx`` string nor within the supported R13-R2018 range.
    """

    def __init__(self, version: str, supported: str = "AC1012 (R13) - AC1032 (R2018)") -> None:
        self.version = version
        self.supported = supported
        super().__init__(f"unsupported DWG version {version!r}; supported range is {supported}")


class UnsupportedObjectError(DwgDecodeError):
    """An object could not be decoded and was preserved as an opaque record.

    This is raised only from strict-mode decoding.  By default unknown or
    unhandled objects (notably proxy entities) are *kept* as raw blobs and no
    exception is raised, matching LibreDWG's behaviour of skipping the body
    while retaining the object.
    """
