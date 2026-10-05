"""Declarative layouts for blocks of holding registers.

A :class:`RegisterLayout` describes how a vendor register map (mixing
signed/unsigned integers, floats and bit flags inside the same block of
holding registers) is decoded into named fields, and how partial field
updates are turned back into an updated register array while preserving
unspecified fields and reserved bits.

Example::

    layout = RegisterLayout(
        [
            FieldDef("temperature", 0, FieldType.INT16),
            FieldDef("pressure", 1, FieldType.FLOAT32, word_order="little"),
            FieldDef("running", 3, FieldType.BITS, bit=0),
            FieldDef("fault", 3, FieldType.BITS, bit=1),
            FieldDef("setpoint", 3, FieldType.BITS, bit=4, width=4, signed=True),
        ],
        nan_policy="error",
    )
    values = layout.decode([0xFF9C, 0x8000, 0x437A, 0x00D3])
    updated = layout.encode({"fault": True}, base=registers)

Overlaps between fields are rejected when the layout is built, except
that disjoint bit fields may share the same register.
"""

from __future__ import annotations

import enum
import math
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .exceptions import ModbusException


__all__ = [
    "DecodeError",
    "EncodeError",
    "FieldDef",
    "FieldType",
    "LayoutError",
    "RegisterLayout",
]


class LayoutError(ModbusException):
    """Illegal layout definition (overlap, bad range, ...)."""


class DecodeError(ModbusException):
    """Register array cannot be decoded (too short, bad values, NaN, ...)."""


class EncodeError(ModbusException):
    """Field values cannot be encoded (overflow, NaN, unknown field, ...)."""


class FieldType(enum.Enum):
    """Supported field types (struct format, registers per field)."""

    INT16 = ("h", 1)
    UINT16 = ("H", 1)
    INT32 = ("i", 2)
    UINT32 = ("I", 2)
    INT64 = ("q", 4)
    UINT64 = ("Q", 4)
    FLOAT32 = ("f", 2)
    FLOAT64 = ("d", 4)
    BITS = ("", 1)


_INT_RANGES: dict[FieldType, tuple[int, int]] = {
    FieldType.INT16: (-(2**15), 2**15 - 1),
    FieldType.UINT16: (0, 2**16 - 1),
    FieldType.INT32: (-(2**31), 2**31 - 1),
    FieldType.UINT32: (0, 2**32 - 1),
    FieldType.INT64: (-(2**63), 2**63 - 1),
    FieldType.UINT64: (0, 2**64 - 1),
}

_FLOAT_TYPES = (FieldType.FLOAT32, FieldType.FLOAT64)


@dataclass(frozen=True)
class FieldDef:
    """Definition of a single named field inside a register block.

    :param name: unique field name
    :param offset: register offset of the field inside the block
    :param type: one of :class:`FieldType`
    :param byte_order: byte order inside each 16 bit register
        (only for non-BITS fields)
    :param word_order: order of the registers for multi-register fields
        (only for non-BITS fields)
    :param bit: first bit (0..15, LSB=0) inside the register (BITS only)
    :param width: number of bits (BITS only, default 1)
    :param signed: interpret a multi-bit BITS field as two's complement
    """

    name: str
    offset: int
    type: FieldType
    byte_order: Literal["big", "little"] = "big"
    word_order: Literal["big", "little"] = "big"
    bit: int | None = None
    width: int = 1
    signed: bool = False


class RegisterLayout:
    """Validated declarative layout of a block of holding registers."""

    def __init__(
        self,
        fields: Sequence[FieldDef],
        *,
        nan_policy: Literal["error", "allow"] = "error",
    ) -> None:
        """Build and validate a layout.

        :param fields: field definitions, see :class:`FieldDef`
        :param nan_policy: "error" raises on NaN float values during
            decode/encode, "allow" passes them through
        :raises LayoutError: on illegal definitions or overlaps
        """
        if nan_policy not in ("error", "allow"):
            raise LayoutError(f"Illegal nan_policy {nan_policy!r}")
        self.nan_policy = nan_policy
        self._fields: dict[str, FieldDef] = {}
        scalar_regs: dict[int, str] = {}
        bit_regs: dict[int, list[tuple[int, int, str]]] = {}
        for field in fields:
            self._validate(field, scalar_regs, bit_regs)
            if field.name in self._fields:
                raise LayoutError(f"Duplicate field name {field.name!r}")
            self._fields[field.name] = field
        used = set(scalar_regs) | set(bit_regs)
        self._span = max(used) + 1 if used else 0

    @staticmethod
    def _validate(
        field: FieldDef,
        scalar_regs: dict[int, str],
        bit_regs: dict[int, list[tuple[int, int, str]]],
    ) -> None:
        if not isinstance(field.type, FieldType):
            raise LayoutError(f"{field.name}: illegal type {field.type!r}")
        if not field.name:
            raise LayoutError("Field name must not be empty")
        if not isinstance(field.offset, int) or field.offset < 0:
            raise LayoutError(f"{field.name}: illegal offset {field.offset!r}")
        if field.byte_order not in ("big", "little"):
            raise LayoutError(f"{field.name}: illegal byte_order {field.byte_order!r}")
        if field.word_order not in ("big", "little"):
            raise LayoutError(f"{field.name}: illegal word_order {field.word_order!r}")
        if field.type is FieldType.BITS:
            if field.bit is None:
                raise LayoutError(f"{field.name}: BITS field requires a bit offset")
            if not 0 <= field.bit < 16:
                raise LayoutError(f"{field.name}: bit {field.bit} outside 0..15")
            if not 1 <= field.width <= 16:
                raise LayoutError(f"{field.name}: width {field.width} outside 1..16")
            if field.bit + field.width > 16:
                raise LayoutError(
                    f"{field.name}: bits {field.bit}..{field.bit + field.width - 1}"
                    " exceed register"
                )
            if field.byte_order != "big" or field.word_order != "big":
                raise LayoutError(
                    f"{field.name}: byte/word order not allowed for BITS fields"
                )
            if field.signed and field.width < 2:
                raise LayoutError(
                    f"{field.name}: signed BITS field requires width >= 2"
                )
            if field.offset in scalar_regs:
                raise LayoutError(
                    f"{field.name}: register {field.offset} already used by"
                    f" field {scalar_regs[field.offset]!r}"
                )
            lo, hi = field.bit, field.bit + field.width
            for other_lo, other_hi, other_name in bit_regs.get(field.offset, []):
                if lo < other_hi and other_lo < hi:
                    raise LayoutError(
                        f"{field.name}: bits {lo}..{hi - 1} overlap with field"
                        f" {other_name!r} in register {field.offset}"
                    )
            bit_regs.setdefault(field.offset, []).append((lo, hi, field.name))
        else:
            if field.bit is not None:
                raise LayoutError(
                    f"{field.name}: bit offset only allowed for BITS fields"
                )
            count = field.type.value[1]
            for reg in range(field.offset, field.offset + count):
                if reg in scalar_regs:
                    raise LayoutError(
                        f"{field.name}: register {reg} already used by field"
                        f" {scalar_regs[reg]!r}"
                    )
                if reg in bit_regs:
                    raise LayoutError(
                        f"{field.name}: register {reg} already holds bit fields"
                    )
                scalar_regs[reg] = field.name

    @property
    def span(self) -> int:
        """Number of registers covered by the layout."""
        return self._span

    @property
    def fields(self) -> tuple[str, ...]:
        """Names of all defined fields."""
        return tuple(self._fields)

    def decode(self, registers: Sequence[int]) -> dict[str, Any]:
        """Decode a register array into a dict of named field values.

        :param registers: register values, at least :attr:`span` entries
        :raises DecodeError: on short input or illegal register values
        """
        regs = self._check_registers(registers, DecodeError)
        result: dict[str, Any] = {}
        for name, field in self._fields.items():
            if field.type is FieldType.BITS:
                result[name] = self._decode_bits(field, regs[field.offset])
            else:
                result[name] = self._decode_scalar(field, regs)
        return result

    def encode(
        self,
        values: Mapping[str, Any],
        base: Sequence[int] | None = None,
    ) -> list[int]:
        """Encode partial field values into an updated register array.

        Fields not present in ``values`` and reserved bits are preserved
        from ``base`` (or cleared when ``base`` is None).

        :param values: mapping of field name to new value
        :param base: current register array, defaults to all zero
        :raises EncodeError: on unknown fields, overflow, NaN or short base
        """
        unknown = set(values) - set(self._fields)
        if unknown:
            raise EncodeError(f"Unknown field(s): {sorted(unknown)}")
        if base is None:
            regs = [0] * self._span
        else:
            regs = self._check_registers(base, EncodeError)
        for name, value in values.items():
            field = self._fields[name]
            if field.type is FieldType.BITS:
                regs[field.offset] = self._encode_bits(
                    field, value, regs[field.offset]
                )
            else:
                self._encode_scalar(field, value, regs)
        return regs

    def _check_registers(
        self, registers: Sequence[int], exc: type[ModbusException]
    ) -> list[int]:
        regs = list(registers)
        if len(regs) < self._span:
            raise exc(
                f"Need at least {self._span} registers, got {len(regs)}"
            )
        for reg in regs:
            if not isinstance(reg, int) or not 0 <= reg <= 0xFFFF:
                raise exc(f"Illegal register value {reg!r}")
        return regs

    def _decode_scalar(self, field: FieldDef, regs: list[int]) -> int | float:
        count = field.type.value[1]
        sel = regs[field.offset : field.offset + count]
        if field.word_order == "little":
            sel = sel[::-1]
        data = b"".join(
            int.to_bytes(reg, 2, field.byte_order) for reg in sel
        )
        value: int | float = struct.unpack(f">{field.type.value[0]}", data)[0]
        if (
            field.type in _FLOAT_TYPES
            and self.nan_policy == "error"
            and math.isnan(value)
        ):
            raise DecodeError(f"{field.name}: decoded value is NaN")
        return value

    def _encode_scalar(
        self, field: FieldDef, value: Any, regs: list[int]
    ) -> None:
        if field.type in _INT_RANGES:
            if not isinstance(value, int):
                raise EncodeError(
                    f"{field.name}: expected int, got {type(value).__name__}"
                )
            lo, hi = _INT_RANGES[field.type]
            if not lo <= value <= hi:
                raise EncodeError(
                    f"{field.name}: value {value} outside {field.type.name}"
                    f" range {lo}..{hi}"
                )
        else:
            if not isinstance(value, (int, float)):
                raise EncodeError(
                    f"{field.name}: expected float, got {type(value).__name__}"
                )
            value = float(value)
            if math.isnan(value) and self.nan_policy == "error":
                raise EncodeError(f"{field.name}: NaN not allowed")
        try:
            data = struct.pack(f">{field.type.value[0]}", value)
        except (OverflowError, struct.error) as exc:
            raise EncodeError(
                f"{field.name}: value {value!r} does not fit {field.type.name}"
            ) from exc
        words = [
            int.from_bytes(data[i : i + 2], field.byte_order)
            for i in range(0, len(data), 2)
        ]
        if field.word_order == "little":
            words = words[::-1]
        regs[field.offset : field.offset + len(words)] = words

    @staticmethod
    def _decode_bits(field: FieldDef, reg: int) -> int | bool:
        assert field.bit is not None
        raw = (reg >> field.bit) & ((1 << field.width) - 1)
        if field.signed and raw >= 1 << (field.width - 1):
            raw -= 1 << field.width
        if field.width == 1 and not field.signed:
            return bool(raw)
        return raw

    @staticmethod
    def _encode_bits(field: FieldDef, value: Any, reg: int) -> int:
        assert field.bit is not None
        if not isinstance(value, int):
            raise EncodeError(
                f"{field.name}: expected int/bool, got {type(value).__name__}"
            )
        if field.signed:
            lo, hi = -(1 << (field.width - 1)), (1 << (field.width - 1)) - 1
        else:
            lo, hi = 0, (1 << field.width) - 1
        if not lo <= value <= hi:
            raise EncodeError(
                f"{field.name}: value {value} outside range {lo}..{hi}"
            )
        mask = ((1 << field.width) - 1) << field.bit
        return (reg & ~mask) | ((int(value) << field.bit) & mask)
