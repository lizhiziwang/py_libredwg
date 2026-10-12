# SPDX-License-Identifier: GPL-3.0-or-later
#
# py-libredwg — pure-Python read-only DWG parser.
# Derivative work of GNU LibreDWG (https://github.com/LibreDWG/libredwg),
# Copyright (C) 2009, 2010, 2015, 2018-2026 Free Software Foundation, Inc.
"""Bit-stream reader — a translation of LibreDWG's ``src/bits.c``.

This is the foundation the whole decoder sits on, so the semantics of
``bit_read_*`` are reproduced exactly.

Bit order
---------
LibreDWG reads every bit **most-significant-bit first**: the bit at stream
position 0 is ``0x80`` of byte 0.  ``bits.h`` documents the layout as::

    position: 01234567 01234567 01234567 ...
    bits:     76543210 76543210 76543210 ...
              \\______/ \\______/ \\______/
               byte 1   byte 2   byte 3  ...

A very common misconception is that pre-R2004 files use LSB-first order and
2004+ files MSB-first.  That is **not** what LibreDWG implements — there is no
version conditional anywhere in ``bit_read_B`` or the bit plumbing.  The bit
order is unconditionally MSB-first; what changes at R2004 is *compression*
(the "MC" modular streams use a different reader, not a different bit order).

State
-----
``byte`` is the index of the current byte and ``bit`` is the sub-byte position,
always in ``0..7``.  Advancing ``n`` bits is exactly::

    byte += (bit + n) >> 3
    bit = (bit + n) & 7

Reading is forward-only.  LibreDWG's decoder essentially never seeks backward,
and neither does this class, though :meth:`seek_bits` is provided for the few
places that reposition to a known absolute section offset.

Signedness
----------
``BS``, ``BL``, ``BLL`` and ``MS`` are **unsigned** in LibreDWG
(``BITCODE_BS`` is ``uint16_t`` and so on); there is no two's-complement logic
in them.  The only signed reader is ``MC``.  Where a DWG field is conceptually
signed (for example lineweight, where ``-1`` means *ByLayer*) signedness comes
from the C struct field type, not the reader — :meth:`read_signed_bits` is
offered for those cases and sign-extends explicitly.

Performance notes (numpy)
-------------------------
The reads here are small and randomly strided: single bits, 2-bit prefixes,
7-bit groups.  Building a numpy array per read would cost roughly a microsecond
against ~50 ns for the equivalent integer arithmetic, so numpy is deliberately
**not** used in the per-bit path.  The optimisations that actually matter are
Python-level: ``__slots__``, a single ``bytes`` buffer loaded once, byte-aligned
fast paths that use ``int.from_bytes`` for multi-byte raw reads, and avoiding
allocation in the hot paths.

numpy *is* used where it genuinely wins — bulk, byte-aligned work such as
materialising a whole decompressed section as a ``uint8`` array
(:func:`as_array`) and the Reed-Solomon / LZ77 passes in :mod:`sections`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from .constants import (
    OFFSET_MIN,
    DwgVersion,
    find_color_index,
    rgb_palette_index,
)
from .errors import BitStreamError

if TYPE_CHECKING:
    # :meth:`BitReader.read_cmc` and :meth:`BitReader.read_enc` return a
    # ``CMColor``, but ``objects.base_object`` imports this module — so the
    # import is for the annotation only, and deferred so it cannot be
    # circular at runtime.
    from .objects.base_object import CMColor

__all__ = ["BitReader", "Handle", "TimeBLL", "as_array", "NAN"]

#: What LibreDWG's ``bit_nan()`` yields in a release build.
NAN: Final[float] = 0.0

#: Largest value ``bit_read_MC`` will produce before it treats the stream as
#: corrupt (``INT32_MAX`` in ``bits.c``).
_MAX_MC: Final[int] = 0x7FFFFFFF


@dataclass(slots=True)
class Handle:
    """A decoded object reference (``Dwg_Handle`` in ``dwg.h``).

    A DWG handle is not a plain integer: the leading byte packs a 4-bit
    ``code`` (how the reference should be resolved) and a 4-bit byte ``size``.
    The value bytes that follow are big-endian on the stream, so
    :attr:`value` is the integer those bytes denote.

    A ``code`` of :data:`~py_libredwg.constants.OFFSET_MIN` (6) or above means
    the value is *relative* — a delta from the handle currently being decoded
    — and must be resolved by :meth:`to_absolute`.  Codes 0–5 carry the
    reference outright; see :meth:`to_absolute` for the full table.
    """

    code: int = 0
    size: int = 0
    value: int = 0
    is_relative: bool = False

    @property
    def is_absolute(self) -> bool:
        return not self.is_relative

    def to_absolute(self, reference: int) -> int:
        """Resolve a handle value against the handle it was stored in.

        Translation of ``dwg_decode_handleref`` (``decode.c``), which is a
        plain switch over the code:

        ================  ==============================================
        code 0, 2, 3, 4, 5  absolute; the stored value *is* the handle
        code 6              ``reference + 1``
        code 8              ``reference - 1``
        code 10             ``reference + value``
        code 12             ``reference - value``
        code 14             ``reference`` (the object's own handle)
        ================  ==============================================

        Codes 2/3 are soft/hard *owners* and 4/5 soft/hard *pointers*; the
        distinction matters to a writer, not to a reader, so they collapse to
        the same absolute result here.
        """
        if not self.is_relative:
            return self.value
        if self.code == 6:
            return reference + 1
        if self.code == 8:
            return reference - 1
        if self.code == 10:
            return reference + self.value
        if self.code == 12:
            return reference - self.value
        if self.code == 14:
            return reference
        # Any other code (something DWG does not define) is unresolvable.
        return 0

    def __bool__(self) -> bool:
        # A zero-length handle is the "null reference" (DWG writes 0 bytes).
        return self.size != 0

    def __int__(self) -> int:
        return self.value


def as_array(data: bytes | bytearray | memoryview):
    """Wrap an immutable buffer as a numpy ``uint8`` view without copying.

    This is the one place numpy is worth using on the read path: whole-section
    buffers handled by the 2004+ decompressor and the Reed-Solomon decoder.
    ``np.frombuffer`` is a view, so no copy is made.
    """
    import numpy as np

    return np.frombuffer(memoryview(data), dtype=np.uint8)


@dataclass(slots=True)
class TimeBLL:
    """A DWG calendar timestamp (``BITCODE_TIMEBLL`` / ``bit_read_TIMEBLL``).

    Two counters: :attr:`days` (days since the Julian epoch) and
    :attr:`ms` (milliseconds since midnight, in 1e-8-second units upstream —
    LibreDWG keeps the raw field and only scales it into :attr:`value`).
    """

    days: int = 0
    ms: int = 0
    value: float = 0.0

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return f"TimeBLL(days={self.days}, ms={self.ms})"


class BitReader:
    """Forward-only MSB-first bit reader over an in-memory buffer.

    Parameters
    ----------
    data:
        The whole buffer.  It is stored once and never copied; the reader
        holds only indices into it.
    version:
        The version being *written* (unused when reading).
    from_version:
        The version the data was read *from*.  This is what drives every
        version-dependent branch, exactly as ``dat->from_version`` does.
    strict:
        When true (the default), reading past the end of the buffer raises
        :class:`~py_libredwg.errors.BitStreamError`.  When false the reader
        emulates LibreDWG's ``CHK_OVERFLOW`` behaviour instead: it records the
        problem, returns the same sentinel value the C code returns, and does
        **not** advance — which desynchronises the stream exactly as the C
        code does.

    Notes
    -----
    Structurally impossible values (a ``BD`` with a reserved ``11`` code, a
    ``BS`` beyond its range) are *not* fatal: LibreDWG logs them and carries
    on, so this class does the same and appends a note to :attr:`problems`.
    Draining the buffer, by contrast, is unrecoverable and is what ``strict``
    controls.
    """

    __slots__ = ("_data", "_size", "byte", "bit", "version", "from_version",
                 "codepage", "strict", "problems", "_n_overflows")

    def __init__(
        self,
        data: bytes | bytearray | memoryview,
        *,
        version: DwgVersion = DwgVersion.R_2000,
        from_version: DwgVersion | None = None,
        codepage: int = 0,
        strict: bool = True,
    ) -> None:
        # Normalise to bytes: indexing a bytes object yields an int, which is
        # what every hot path wants.  A bytearray would also work but bytes
        # avoids accidental mutation of the underlying file image.
        self._data = data if isinstance(data, bytes) else bytes(data)
        self._size = len(self._data)
        self.byte = 0
        self.bit = 0
        self.version = version
        self.from_version = version if from_version is None else from_version
        self.codepage = codepage
        self.strict = strict
        self.problems: list[str] = []
        self._n_overflows = 0

    # -- construction helpers --------------------------------------------
    def clone(self, data: bytes, *, strict: bool | None = None) -> BitReader:
        """A new reader over ``data`` inheriting this reader's version context.

        Mirrors ``bit_chain_init_dat`` + ``bit_chain_set_version``: the
        derived chain carries ``version``, ``from_version`` and ``codepage``
        across.  Used for the separate handle / string sub-streams of an
        object.
        """
        return BitReader(
            data,
            version=self.version,
            from_version=self.from_version,
            codepage=self.codepage,
            strict=self.strict if strict is None else strict,
        )

    # -- position --------------------------------------------------------
    @property
    def size(self) -> int:
        """Length of the underlying buffer in bytes."""
        return self._size

    @property
    def data(self) -> bytes:
        return self._data

    def bit_position(self) -> int:
        """Absolute position in bits (``bit_position`` in ``bits.c``)."""
        return (self.byte * 8) + (self.bit & 7)

    def tell_bits(self) -> int:
        return self.bit_position()

    def at_end(self) -> bool:
        return self.byte >= self._size

    def remaining_bits(self) -> int:
        return max(0, (self._size * 8) - self.bit_position())

    def advance(self, nbits: int) -> None:
        """Move forward ``nbits`` bits (``bit_advance_position``)."""
        pos = self.bit + nbits
        self.byte += pos >> 3
        self.bit = pos & 7

    #: LibreDWG-style alias — the C symbol is ``bit_advance_position``.
    def skip_bits(self, nbits: int) -> None:
        self.advance(nbits)

    def align_to_byte(self) -> int:
        """Advance to the next byte boundary; return how many bits were skipped.

        Translation of LibreDWG's ``bit_chain_align`` / the ``if (dat->bit)
        bit_advance_position (dat, 8 - dat->bit)`` idiom.  Returns 0 when
        already aligned.
        """
        if self.bit:
            skipped = 8 - self.bit
            self.byte += 1
            self.bit = 0
            return skipped
        return 0

    def seek_bits(self, bitpos: int) -> None:
        """Reposition to an absolute bit offset (``bit_set_position``).

        Only forward seeks are expected by the decoder; the guard below exists
        so that a bad section offset is reported rather than silently read
        from the wrong place.
        """
        if bitpos < 0:
            raise BitStreamError(f"negative seek position {bitpos}")
        if bitpos > self._size * 8:
            self._overflow(f"seek to bit {bitpos} past end ({self._size * 8})")
            return
        self.byte = bitpos >> 3
        self.bit = bitpos & 7

    def seek_bytes(self, offset: int) -> None:
        """Reposition to a byte offset (convenience over :meth:`seek_bits`)."""
        self.seek_bits(offset * 8)

    # -- overflow handling -----------------------------------------------
    def _overflow(self, what: str, retval=0):
        """Handle a read that runs off the end of the buffer.

        In strict mode this raises; otherwise it records the problem and
        returns ``retval`` *without advancing*, matching ``CHK_OVERFLOW``.
        """
        self._n_overflows += 1
        msg = (f"{what} at byte {self.byte}.{self.bit} "
               f"(buffer is {self._size} bytes)")
        if self.strict:
            raise BitStreamError(msg, offset=self.byte)
        self.problems.append(msg)
        return retval

    def _note(self, message: str) -> None:
        """Record a non-fatal structural anomaly (LibreDWG logs and continues)."""
        self.problems.append(message)

    def _check_room(self, nbytes: int) -> bool:
        """True when ``nbytes`` more whole bytes are available from here."""
        if self.byte + nbytes > self._size:
            return False
        return True

    # ------------------------------------------------------------------
    # Single-bit and bit-field reads
    # ------------------------------------------------------------------
    def read_bit(self) -> int:
        """Read one bit (``bit_read_B``).

        This is the hottest method in the library.  It is written to avoid
        attribute lookups: everything is hoisted into locals and the
        increment-branch is only taken on the byte boundary.
        """
        i = self.byte
        if i >= self._size:
            return self._overflow("bit_read_B")
        v = (self._data[i] >> (7 - self.bit)) & 1
        b = self.bit + 1
        if b == 8:
            self.byte = i + 1
            self.bit = 0
        else:
            self.bit = b
        return v

    # LibreDWG-style alias.
    bit_read_B = read_bit

    def read_2bits(self) -> int:
        """Read a 2-bit field (``bit_read_BB``).

        The first bit read is the *most significant* of the pair.  The
        straddling case (``bit == 7``) is the reason this is not implemented
        as two calls to :meth:`read_bit`.
        """
        i = self.byte
        b = self.bit
        if i >= self._size:
            return self._overflow("bit_read_BB")
        if b < 7:
            result = (self._data[i] >> (6 - b)) & 0x03
            b += 2
            if b == 8:
                # The pair ended exactly on the byte boundary.  Leaving
                # ``bit == 8`` here would make the next read shift by a
                # negative amount, so the byte advance must happen now.
                self.byte = i + 1
                self.bit = 0
            else:
                self.bit = b
            return result
        # Straddles the byte boundary: low bit here, high bit of the next byte.
        result = (self._data[i] & 0x01) << 1
        if i + 1 < self._size:
            result |= (self._data[i + 1] & 0x80) >> 7
        else:
            self._note("bit_read_BB straddled the final byte")
        self.byte = i + 1
        self.bit = 1
        return result

    bit_read_BB = read_2bits

    def read_4bits(self) -> int:
        """Read a 4-bit nibble (``bit_read_4BITS``)."""
        d = self._data
        i = self.byte
        if i >= self._size:
            return self._overflow("bit_read_4BITS")
        b = self.bit
        if b == 0:
            result = (d[i] >> 4) & 0x0F
            self.byte = i + 1
            return result
        if b <= 4:
            result = (d[i] >> (4 - b)) & 0x0F
            b += 4
            if b == 8:
                # Nibble ended on the byte boundary — see read_2bits.
                self.byte = i + 1
                self.bit = 0
            else:
                self.bit = b
            return result
        # Straddles: (8 - b) bits remain in this byte, the rest come next.
        take = 8 - b
        result = (d[i] & ((1 << take) - 1)) << (4 - take)
        if i + 1 < self._size:
            result |= (d[i + 1] >> (8 - (4 - take))) & ((1 << (4 - take)) - 1)
        else:
            self._note("bit_read_4BITS straddled the final byte")
        self.byte = i + 1
        self.bit = 4 - take
        return result

    bit_read_4BITS = read_4bits

    def read_bits(self, nbits: int) -> int:
        """Read ``nbits`` as an unsigned integer, MSB-first.

        There is no single C counterpart: LibreDWG spells these out as repeated
        ``bit_read_B`` chains.  This generalisation is bit-for-bit equivalent
        for the widths the format uses (up to 64).

        If the field straddles bytes this loops; a field fully inside the
        current byte is handled without touching the position bookkeeping
        beyond one add.
        """
        if nbits <= 0:
            return 0
        b = self.bit
        i = self.byte
        if b + nbits <= 8:
            # Fast path: the whole field lies within one byte.
            if i >= self._size:
                return self._overflow(f"read_bits({nbits})")
            result = (self._data[i] >> (8 - b - nbits)) & ((1 << nbits) - 1)
            b += nbits
            if b == 8:
                self.byte = i + 1
                self.bit = 0
            else:
                self.bit = b
            return result

        # General path: consume whole bytes, then the tail.
        result = 0
        remaining = nbits
        while remaining > 0:
            avail = 8 - self.bit
            take = avail if avail <= remaining else remaining
            result = (result << take) | self._read_partial(take)
            remaining -= take
        return result

    def _read_partial(self, take: int) -> int:
        """Read ``take`` (1..8) bits from the current position, MSB-first."""
        i = self.byte
        if i >= self._size:
            self._overflow(f"read_bits chunk of {take}")
            return 0
        b = self.bit
        result = (self._data[i] >> (8 - b - take)) & ((1 << take) - 1)
        b += take
        if b == 8:
            self.byte = i + 1
            self.bit = 0
        else:
            self.bit = b
        return result

    def read_signed_bits(self, nbits: int) -> int:
        """Read ``nbits`` and sign-extend as two's complement.

        This is a py-libredwg extension, not a LibreDWG primitive: the C
        library's ``BS``/``BL`` are unsigned and signedness comes from the
        struct field type.  Provided for fields the spec spells out as signed
        (lineweight is the usual one) so the conversion is explicit rather
        than smeared into call sites.
        """
        if nbits <= 0:
            return 0
        value = self.read_bits(nbits)
        sign = 1 << (nbits - 1)
        if value & sign:
            value -= (1 << nbits)
        return value

    # ------------------------------------------------------------------
    # Raw (byte-aligned) reads
    # ------------------------------------------------------------------
    def read_rc(self) -> int:
        """Read one raw byte (``bit_read_RC``)."""
        i = self.byte
        if i >= self._size:
            return self._overflow("bit_read_RC")
        if self.bit == 0:
            self.byte = i + 1
            return self._data[i]
        # Unaligned: take the 8 bits starting at `bit`, MSB-first.
        b = self.bit
        result = (self._data[i] << b) & 0xFF
        if i + 1 < self._size:
            result |= self._data[i + 1] >> (8 - b)
        else:
            self._note("bit_read_RC straddled the final byte")
        self.byte = i + 1
        return result

    bit_read_RC = read_rc

    def read_rs(self) -> int:
        """Read a raw 16-bit little-endian value (``bit_read_RS``)."""
        if self.bit == 0:
            i = self.byte
            if i + 2 > self._size:
                return self._overflow("bit_read_RS")
            self.byte = i + 2
            return int.from_bytes(self._data[i:i + 2], "little")
        b1 = self.read_rc()
        b2 = self.read_rc()
        return (b2 << 8) | b1

    bit_read_RS = read_rs

    def read_rl(self) -> int:
        """Read a raw 32-bit little-endian value (``bit_read_RL``)."""
        if self.bit == 0:
            i = self.byte
            if i + 4 > self._size:
                return self._overflow("bit_read_RL")
            self.byte = i + 4
            return int.from_bytes(self._data[i:i + 4], "little")
        w1 = self.read_rs()
        w2 = self.read_rs()
        return (w2 << 16) | w1

    bit_read_RL = read_rl

    def read_rll(self) -> int:
        """Read a raw 64-bit little-endian value (``bit_read_RLL``)."""
        if self.bit == 0:
            i = self.byte
            if i + 8 > self._size:
                return self._overflow("bit_read_RLL")
            self.byte = i + 8
            return int.from_bytes(self._data[i:i + 8], "little")
        w1 = self.read_rl()
        w2 = self.read_rl()
        return (w2 << 32) | w1

    bit_read_RLL = read_rll

    def read_rd(self) -> float:
        """Read a raw IEEE-754 double (``bit_read_RD``), little-endian."""
        if self.bit == 0:
            i = self.byte
            if i + 8 > self._size:
                self._overflow("bit_read_RD")
                return NAN
            self.byte = i + 8
            return struct.unpack_from("<d", self._data, i)[0]
        bits = self.read_rll()
        return struct.unpack("<d", struct.pack("<Q", bits))[0]

    bit_read_RD = read_rd

    def read_bytes(self, nbytes: int) -> bytes:
        """Read ``nbytes`` bytes, fast-pathing the byte-aligned case.

        Equivalent to LibreDWG's ``bit_read_fixed``.  The aligned path is a
        slice (one allocation); the unaligned path falls back to per-byte
        reads because the bits must be re-windowed.
        """
        if nbytes < 0:
            raise BitStreamError(f"negative byte count {nbytes}")
        if nbytes == 0:
            return b""
        if self.bit == 0:
            i = self.byte
            if i + nbytes > self._size:
                return self._overflow(f"read_bytes({nbytes})", b"")
            self.byte = i + nbytes
            return self._data[i:i + nbytes]
        return bytes(self.read_rc() for _ in range(nbytes))

    bit_read_fixed = read_bytes

    def read_tf(self, length: int) -> bytes:
        """Read a fixed-length text blob (``bit_read_TF``)."""
        return self.read_bytes(length)

    # ------------------------------------------------------------------
    # Compacted ("bit") reads
    # ------------------------------------------------------------------
    def read_bs(self) -> int:
        """Read a bit short (``bit_read_BS``) — unsigned.

        Layout is a 2-bit selector followed by the payload::

            00 -> RS    (16 bits)   total 18
            01 -> RC    ( 8 bits)   total 10
            10 -> the value 0       total  2
            11 -> the value 256     total  2
        """
        code = self.read_2bits()
        if code == 0:
            return self.read_rs()
        if code == 1:
            return self.read_rc() & 0xFF
        if code == 2:
            return 0
        return 256

    bit_read_BS = read_bs

    def read_bl(self) -> int:
        """Read a bit long (``bit_read_BL``) — unsigned.

        Like :meth:`read_bs` but the wide form is 32-bit::

            00 -> RL    (32 bits)   total 34
            01 -> RC    ( 8 bits)   total 10
            10 -> the value 0       total  2
            11 -> invalid

        Note the asymmetry with ``BS``: code ``11`` is a *format error* here,
        not the value 256.  LibreDWG logs it and returns 256; we do the same
        but record it in :attr:`problems`.
        """
        code = self.read_2bits()
        if code == 0:
            return self.read_rl()
        if code == 1:
            return self.read_rc() & 0xFF
        if code == 2:
            return 0
        self._note("bit_read_BL: reserved 2-bit code '11'")
        return 256

    bit_read_BL = read_bl

    def read_bot(self) -> int:
        """Read an object type (``bit_read_BOT``) — R2010 and later only.

        R2010 replaced the plain ``BS`` object-type field with this encoding,
        which is optimised for the fact that almost every type is either a
        small fixed code or one of the R2010+ class types that live at
        ``0x1f0`` upward::

            00 -> RC                  (the type directly)
            01 -> RC + 0x1f0          (a short class type)
            else -> RS                (any other type)
        """
        code = self.read_2bits()
        if code == 0:
            return self.read_rc()
        if code == 1:
            return self.read_rc() + 0x1F0
        return self.read_rs()

    bit_read_BOT = read_bot

    def read_bll(self) -> int:
        """Read a bit long long (``bit_read_BLL``) — unsigned.

        A 3-bit length selector (``(BB << 1) | B``) chooses the form::

            1 -> RC                   ( 8 bits)
            2 -> RS                   (16 bits)
            4 -> RL                   (32 bits)
            n -> n raw bytes, least-significant byte first

        The variable-length case follows ``bit_read_BLL``: upstream assembles
        the bytes big-endian into a 64-bit word and then byte-swaps it with
        ``be64toh``, which for a little-endian machine is identical to reading
        the bytes least-significant first.
        """
        length = (self.read_2bits() << 1) | self.read_bit()
        if length == 1:
            return self.read_rc()
        if length == 2:
            return self.read_rs()
        if length == 4:
            return self.read_rl()
        result = 0
        for i in range(length):
            result |= self.read_rc() << (8 * i)
        return result

    bit_read_BLL = read_bll

    def read_bd(self) -> float:
        """Read a bit double (``bit_read_BD``).

        ``00`` reads a full 64-bit double (66 bits total), ``01`` is the
        literal ``1.0`` and ``10`` the literal ``0.0`` — this is how DWG
        compresses the very common coordinates.  ``11`` is reserved and yields
        LibreDWG's NaN sentinel.
        """
        code = self.read_2bits()
        if code == 0:
            return self.read_rd()
        if code == 1:
            return 1.0
        if code == 2:
            return 0.0
        self._note("bit_read_BD: reserved 2-bit code '11'")
        return NAN

    bit_read_BD = read_bd

    def read_dd(self, default: float) -> float:
        """Read a bit double with a default (``bit_read_DD``).

        The 2-bit selector decides how much of ``default`` is replaced::

            00 -> default unchanged            (2 bits)
            01 -> 4 low bytes replaced         (34 bits)
            10 -> 6 low bytes replaced         (50 bits)
            11 -> full double, default ignored (66 bits)

        The partial forms splice stream bytes into the *low* end of the
        default's IEEE-754 representation and keep its high bytes, which is
        how DWG stores coordinates clustered near a previous value.
        """
        code = self.read_2bits()
        if code == 0:
            return default
        if code == 3:
            return self.read_rd()
        # Work on the raw little-endian byte image of the default.
        buf = bytearray(struct.pack("<d", default))
        if code == 2:
            # Replace bytes 4,5 then 0,1,2,3 — i.e. keep the top two bytes.
            buf[4] = self.read_rc()
            buf[5] = self.read_rc()
            buf[0] = self.read_rc()
            buf[1] = self.read_rc()
            buf[2] = self.read_rc()
            buf[3] = self.read_rc()
        else:  # code == 1: replace the low 4 bytes only.
            buf[0] = self.read_rc()
            buf[1] = self.read_rc()
            buf[2] = self.read_rc()
            buf[3] = self.read_rc()
        return struct.unpack("<d", bytes(buf))[0]

    bit_read_DD = read_dd

    def read_bt(self) -> float:
        """Read a bit thickness (``bit_read_BT``).

        From R2000 onwards a single flag bit precedes the value; a set flag
        means the thickness is exactly ``0.0`` (the overwhelmingly common
        case) and no further bits are consumed.  Before R2000 there is no flag
        and the value is always a plain ``BD``.
        """
        if self.from_version >= DwgVersion.R_2000:
            mode = self.read_bit()
            return 0.0 if mode else self.read_bd()
        return self.read_bd()

    bit_read_BT = read_bt

    def read_be(self) -> tuple[float, float, float]:
        """Read an extrusion direction (``bit_read_BE``).

        From R2000 onwards a flag bit selects the canonical ``(0, 0, 1)``
        extrusion without storing three doubles.  When x and y are both zero
        the z component is normalised to a clean ``+-1.0``.
        """
        if self.from_version >= DwgVersion.R_2000 and self.read_bit():
            return (0.0, 0.0, 1.0)
        x = self.read_bd()
        y = self.read_bd()
        z = self.read_bd()
        if x == 0.0 and y == 0.0:
            z = -1.0 if z <= 0.0 else 1.0
        return (x, y, z)

    bit_read_BE = read_be

    # ------------------------------------------------------------------
    # Modular reads
    # ------------------------------------------------------------------
    def read_mc(self) -> int:
        """Read a signed modular char (``bit_read_MC``).

        The value is stored in 7-bit groups, **least-significant group
        first**.  Bit 7 of each byte is a continuation flag; the last byte's
        bit 6 is a sign flag.  Up to five bytes (35 raw bits) are read, but a
        magnitude above ``INT32_MAX`` is treated as corruption.
        """
        result = 0
        shift = 0
        negative = False
        for _ in range(5):
            b = self.read_rc()
            if b & 0x80:
                result |= (b & 0x7F) << shift
            else:
                if b & 0x40:
                    negative = True
                    b &= 0x3F
                result |= b << shift
                if result > _MAX_MC:
                    self._note("bit_read_MC: value exceeds INT32_MAX")
                    return 0
                return -result if negative else result
            shift += 7
        self._note("bit_read_MC: unterminated modular value")
        return 0

    bit_read_MC = read_mc

    def read_umc(self) -> int:
        """Read an unsigned modular char (``bit_read_UMC``).

        Same 7-bit-group scheme as :meth:`read_mc` but up to eight bytes and
        with no sign bit — bit 6 of the final byte is ordinary data.  Used for
        large handle deltas.
        """
        result = 0
        shift = 0
        for _ in range(8):
            b = self.read_rc()
            if b & 0x80:
                result |= (b & 0x7F) << shift
            else:
                return result | (b << shift)
            shift += 7
        self._note("bit_read_UMC: unterminated modular value")
        return 0

    bit_read_UMC = read_umc

    def read_ms(self) -> int:
        """Read a modular short (``bit_read_MS``) — unsigned.

        The unit is a 16-bit little-endian word carrying 15 value bits; bit 15
        is the continuation flag.  At most two words, so at most 30 bits.  A
        third continuation bit is an error and yields 0, discarding the two
        words already consumed — LibreDWG returns 0 rather than a partial value.
        """
        word = self.read_rs()
        if not (word & 0x8000):
            return word
        result = word & 0x7FFF
        word = self.read_rs()
        if word & 0x8000:
            self._note("bit_read_MS: more than two words")
            return 0
        return result | (word << 15)

    bit_read_MS = read_ms

    def read_bsd(self) -> int:
        """Read a *signed* bit short (``(BITCODE_BSd)bit_read_BS``).

        ``BS`` itself is unsigned; the signed variants (``TREEDEPTH``,
        ``USERI1``..``USERI5``) reinterpret the same 16-bit payload as
        ``int16_t``, so only the wide ``RS`` form can go negative.  ``BS``
        never yields anything outside ``0..65535``, so the fix-up is exact.
        """
        value = self.read_bs()
        return value - 0x10000 if value > 0x7FFF else value

    bit_read_BSd = read_bsd

    def read_bld(self) -> int:
        """Read a *signed* bit long (``(BITCODE_BLd)bit_read_BL``)."""
        value = self.read_bl()
        return value - 0x100000000 if value > 0x7FFFFFFF else value

    bit_read_BLd = read_bld

    def read_rld(self) -> int:
        """Read a *signed* raw long (``(BITCODE_RLd)bit_read_RL``)."""
        value = self.read_rl()
        return value - 0x100000000 if value > 0x7FFFFFFF else value

    bit_read_RLd = read_rld

    # ------------------------------------------------------------------
    # Repeated raw/bit values (the ``FIELD_2RD`` / ``FIELD_3BD`` family)
    # ------------------------------------------------------------------
    # Upstream these are macros expanding to N calls of the scalar reader;
    # they are collected here because nearly every geometry field uses one.
    def read_2rd(self) -> tuple[float, float]:
        """Two raw doubles (``FIELD_2RD``)."""
        return (self.read_rd(), self.read_rd())

    def read_3rd(self) -> tuple[float, float, float]:
        """Three raw doubles (``FIELD_3RD``)."""
        return (self.read_rd(), self.read_rd(), self.read_rd())

    def read_2bd(self) -> tuple[float, float]:
        """Two bit doubles (``FIELD_2BD``)."""
        return (self.read_bd(), self.read_bd())

    def read_3bd(self) -> tuple[float, float, float]:
        """Three bit doubles (``FIELD_3BD``) — the standard DWG point."""
        return (self.read_bd(), self.read_bd(), self.read_bd())

    def read_2dd(self, default: float) -> tuple[float, float]:
        """Two defaults-preserving doubles (``FIELD_2DD``).

        Each component is seeded with the *same* default, matching upstream:
        ``FIELD_2DD`` expands to ``bit_read_DD (dat, default)`` twice with the
        default passed in unchanged both times.
        """
        return (self.read_dd(default), self.read_dd(default))

    def read_3dd(self, default: float) -> tuple[float, float, float]:
        """Three defaults-preserving doubles (``FIELD_3DD``)."""
        return (self.read_dd(default), self.read_dd(default), self.read_dd(default))

    def read_timebll(self) -> TimeBLL:
        """Read a timestamp (``bit_read_TIMEBLL``).

        From R13b1 both counters are ``BL``; before that they were plain
        ``RL``s.  Upstream then computes ``value = days + ms * 1e-8``; the
        scale is odd but reproduced verbatim, because callers that print the
        timestamp use exactly this number.
        """
        if self.from_version < DwgVersion.R_13b1:
            days = self.read_rl()
            ms = self.read_rl()
        else:
            days = self.read_bl()
            ms = self.read_bl()
        return TimeBLL(days=days, ms=ms, value=days + (ms * 1e-8))

    bit_read_TIMEBLL = read_timebll

    # ------------------------------------------------------------------
    # Handles
    # ------------------------------------------------------------------
    def read_h(self) -> Handle:
        """Read an object handle (``bit_read_H``).

        The leading byte packs two 4-bit fields for R13b1 and later: the low
        nibble is the byte count of the value and the high nibble is the
        reference code.  Byte counts above 8 and codes above 14 are rejected
        as invalid.  Before R13b1 the whole byte is the byte count and the
        code is implicitly 0.

        The value bytes are read **most-significant byte first**, so
        ``01 02`` denotes 0x0102, not 0x0201.
        """
        if self.byte >= self._size:
            self._overflow("bit_read_H")
            return Handle()
        code_byte = self.read_rc()
        is_relative = False
        if self.from_version < DwgVersion.R_13b1:
            size = code_byte
            code = 0
        else:
            size = code_byte & 0x0F
            code = (code_byte >> 4) & 0x0F

        if size > 8 or code > 14:
            self._note(f"bit_read_H: invalid handle code={code} size={size}")
            return Handle(code=code, size=0, value=0)

        value = 0
        for _ in range(size):
            value = (value << 8) | self.read_rc()

        if code >= OFFSET_MIN:
            is_relative = True
        return Handle(code=code, size=size, value=value, is_relative=is_relative)

    bit_read_H = read_h

    # ------------------------------------------------------------------
    # Strings
    # ------------------------------------------------------------------
    def read_tv(self) -> str:
        """Read a length-prefixed ASCII string (``bit_read_TV``).

        The length prefix is a raw ``RS`` before R13b1 and a compacted ``BS``
        from R13b1 on.  The bytes are then decoded with the file's codepage,
        falling back to latin-1 so that a bad codepage can never raise — DWG
        files in the wild routinely contain mislabelled byte sequences.
        """
        if self.from_version < DwgVersion.R_13b1:
            length = self.read_rs()
        else:
            length = self.read_bs()
        if length == 0:
            return ""
        if length > self._size:
            self._note(f"bit_read_TV: implausible length {length}")
            return ""
        raw = self.read_bytes(length)
        return self._decode_bytes(raw)

    bit_read_TV = read_tv

    def _decode_bytes(self, raw: bytes) -> str:
        if not raw:
            return ""
        # DWG strings are frequently NUL-terminated inside their length.
        raw = raw.split(b"\x00", 1)[0]
        if not raw:
            return ""
        encoding = "latin-1"
        if self.codepage:
            from .codepages import codepage_to_python
            encoding = codepage_to_python(self.codepage)
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            return raw.decode("latin-1", errors="replace")

    def read_tu(self) -> str:
        """Read a UCS-2 string (``bit_read_TU``).

        The length prefix is a ``BS`` counting *characters*, each of which is
        a 16-bit little-endian code unit.  Used for R2007+ DWG data.
        """
        length = self.read_bs()
        if length == 0:
            return ""
        if length * 2 > self._size:
            self._note(f"bit_read_TU: implausible length {length}")
            return ""
        raw = self._read_utf16le(length)
        return raw.rstrip("\x00")

    bit_read_TU = read_tu

    def _read_utf16le(self, nchars: int) -> str:
        """Read ``nchars`` 16-bit little-endian code units and decode them."""
        if self.bit == 0:
            i = self.byte
            nbytes = nchars * 2
            if i + nbytes > self._size:
                self._overflow(f"read_tu({nchars})")
                return ""
            self.byte = i + nbytes
            raw = self._data[i:i + nbytes]
        else:
            buf = bytearray(nchars * 2)
            for k in range(nchars * 2):
                buf[k] = self.read_rc()
            raw = bytes(buf)
        try:
            return raw.decode("utf-16-le")
        except UnicodeDecodeError:
            return raw.decode("utf-16-le", errors="replace")

    def read_t(self) -> str:
        """Read a text value, dispatching on version (``bit_read_T``).

        R2007 and later store text as UCS-2; earlier files store single-byte
        characters.  This mirrors ``IS_FROM_TU``.
        """
        if self.from_version >= DwgVersion.R_2007:
            return self.read_tu()
        return self.read_tv()

    bit_read_T = read_t

    # ------------------------------------------------------------------
    # Colour
    # ------------------------------------------------------------------
    def read_cmc(self, str_dat: BitReader | None = None) -> CMColor:
        """Read a colour (``bit_read_CMC``).

        Before R2004 a colour is just an index into the AutoCAD colour table.
        From R2004 on a true-colour ``BL`` follows, whose top byte is the
        colour method and which may be followed by a name and/or book name
        selected by a flag byte.

        ``str_dat`` is the object's *string sub-stream*.  LibreDWG reads the
        optional colour name and book name from there, not from the main
        object stream, because names are pooled separately.  Passing ``None``
        reads them from this stream, which is only correct for objects whose
        string section is empty.
        """
        from .objects.base_object import CMColor

        str_dat = self if str_dat is None else str_dat
        color = CMColor()
        if self.from_version < DwgVersion.R_13b1:
            color.index = self.read_rs()
        else:
            color.index = self.read_bs()

        if self.from_version >= DwgVersion.R_2004:
            color.rgb = self.read_bl()
            color.method = color.rgb >> 0x18
            color.flag = self.read_rc()
            if color.flag < 4:
                if color.flag & 1:
                    color.name = str_dat.read_t()
                if color.flag & 2:
                    color.book_name = str_dat.read_t()
            else:
                self._note(f"bit_read_CMC: bad colour flag {color.flag}")
                color.flag = 0
            if not (0xC0 <= color.method <= 0xC8):
                # Upstream normalises an unknown method to ACAD_INDEX *and*
                # stamps it into the RGB word, which the palette lookup below
                # then consumes.
                self._note(f"bit_read_CMC: bad colour method 0x{color.method:x}")
                color.method = 0xC2
                color.rgb = 0xC2000000 | (color.rgb & 0xFFFFFF)
            # LibreDWG re-derives the index from the true-colour value.
            color.index = nearest_aci(color.rgb & 0xFFFFFF)
        return color

    bit_read_CMC = read_cmc

    def read_enc(self, hdl_dat: BitReader | None = None) -> CMColor:
        """Read an entity colour (``bit_read_ENC``, R2004+).

        The high byte of the ``BS`` index is a flag byte, so a colour can
        carry a true-colour value, a DBCOLOR handle and an alpha channel.
        The DBCOLOR handle lives in the object's *handle sub-stream*.
        """
        from .objects.base_object import CMColor

        color = CMColor()
        idx = self.read_bs()
        color.index = idx
        if self.from_version >= DwgVersion.R_2004:
            flag = (idx >> 8) & 0xFF
            color.index = idx & 0x1FF
            if flag & 0x80:
                color.rgb = self.read_bl()
            if flag & 0x40:
                color.handle = (hdl_dat or self).read_h()
            if flag & 0x20:
                alpha = self.read_bl()
                color.alpha_type = alpha & 0xFF
                color.alpha = alpha >> 8
            color.flag = flag
        return color

    bit_read_ENC = read_enc


# ---------------------------------------------------------------------------
# Colour-table helpers
# ---------------------------------------------------------------------------
# The ACI palette itself lives in :mod:`py_libredwg.constants` as
# :data:`~py_libredwg.constants.RGB_PALETTE`, transcribed whole from
# ``rgb_palette[256]`` in ``src/dwg.c``.  Only the two conversions belong here,
# next to the readers that call them.


def nearest_aci(rgb: int) -> int:
    """Map a 24-bit true colour back to an ACI index.

    This is ``dwg_find_color_index``: an exact search over the palette, since
    ``bit_read_CMC`` only ever asks about colours that came *from* a palette
    entry.  A colour outside the palette yields 256 (*ByLayer*), not the
    closest entry — upstream returns 256 rather than guessing, and a guess here
    would silently turn an arbitrary RGB into a plausible-looking wrong index.
    """
    return find_color_index(rgb & 0xFFFFFF)


def aci_to_rgb(index: int) -> int:
    """The packed ``0xRRGGBB`` of an ACI index (``dwg_rgb_palette_index``)."""
    return rgb_palette_index(index)
