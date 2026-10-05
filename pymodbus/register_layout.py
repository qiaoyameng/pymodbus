"""Declarative layout for named fields in holding registers.

Example:
    layout = RegisterLayout(
        [
            BitField("enabled", register=0, start_bit=0),
            BitField("alarm", register=0, start_bit=1),
            Field("temperature", 1, RegisterDataType.FLOAT32, word_order="little"),
        ]
    )
    values = layout.decode(registers)
    updated_registers = layout.update(registers, enabled=True)
"""

from __future__ import annotations

import enum
import math
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

ByteOrder: TypeAlias = Literal["big", "little"]
WordOrder: TypeAlias = Literal["big", "little"]
MAX_HOLDING_REGISTERS = 65536


class RegisterDataType(enum.Enum):
    """Numeric data types supported by a register field."""

    INT16 = ("h", 1, False)
    UINT16 = ("H", 1, False)
    INT32 = ("i", 2, False)
    UINT32 = ("I", 2, False)
    INT64 = ("q", 4, False)
    UINT64 = ("Q", 4, False)
    FLOAT32 = ("f", 2, True)
    FLOAT64 = ("d", 4, True)

    def __init__(self, struct_format: str, word_count: int, is_float: bool) -> None:
        """Initialize the data type metadata."""
        self.struct_format = struct_format
        self.word_count = word_count
        self.is_float = is_float


class NaNPolicy(enum.Enum):
    """Policy used when a floating-point field contains NaN."""

    REJECT = "reject"
    ALLOW = "allow"


class RegisterLayoutError(ValueError):
    """Base class for register layout errors."""


class RegisterLayoutDefinitionError(RegisterLayoutError):
    """The declared field layout is invalid."""


class RegisterLayoutValueError(RegisterLayoutError):
    """A register array or field value is invalid."""


class RegisterLayoutOverflowError(RegisterLayoutError, OverflowError):
    """A numeric value does not fit in its declared field."""


class RegisterLayoutNaNError(RegisterLayoutValueError):
    """A NaN was used while the field policy rejects NaN."""


@dataclass(frozen=True)
class Field:
    """A numeric field occupying one or more complete 16-bit registers."""

    name: str
    offset: int
    datatype: RegisterDataType
    byte_order: ByteOrder = "big"
    word_order: WordOrder = "big"
    array_size: int = 1
    nan_policy: NaNPolicy = NaNPolicy.REJECT

    def __post_init__(self) -> None:
        """Validate field parameters."""
        _validate_field(self)


@dataclass(frozen=True)
class BitField:
    """A bit field contained in one register."""

    name: str
    register: int
    start_bit: int
    bit_count: int = 1
    signed: bool = False

    def __post_init__(self) -> None:
        """Validate bit field parameters."""
        _validate_bit_field(self)


RegisterField: TypeAlias = Field | BitField


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise RegisterLayoutDefinitionError("Field names must be non-empty strings.")


def _validate_positive_int(value: int, description: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RegisterLayoutDefinitionError(f"{description} must be a positive int.")


def _validate_endian(value: str, description: str) -> None:
    if value not in ("big", "little"):
        raise RegisterLayoutDefinitionError(
            f"{description} must be 'big' or 'little', got {value!r}."
        )


def _validate_field(field: Field) -> None:
    _validate_name(field.name)
    if isinstance(field.offset, bool) or not isinstance(field.offset, int):
        raise RegisterLayoutDefinitionError("Field offset must be an int.")
    if field.offset < 0:
        raise RegisterLayoutDefinitionError("Field offset cannot be negative.")
    if not isinstance(field.datatype, RegisterDataType):
        raise RegisterLayoutDefinitionError(
            "Field datatype must be a RegisterDataType member."
        )
    _validate_endian(field.byte_order, "byte_order")
    _validate_endian(field.word_order, "word_order")
    _validate_positive_int(field.array_size, "array_size")
    if not isinstance(field.nan_policy, NaNPolicy):
        raise RegisterLayoutDefinitionError(
            "nan_policy must be a NaNPolicy member."
        )


def _validate_bit_field(field: BitField) -> None:
    _validate_name(field.name)
    for member_name in ("register", "start_bit", "bit_count"):
        value = getattr(field, member_name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise RegisterLayoutDefinitionError(f"{member_name} must be an int.")
    if field.register < 0 or field.start_bit < 0:
        raise RegisterLayoutDefinitionError(
            "BitField register and start_bit cannot be negative."
        )
    if field.register >= MAX_HOLDING_REGISTERS:
        raise RegisterLayoutDefinitionError(
            f"BitField register must be below {MAX_HOLDING_REGISTERS}."
        )
    if field.bit_count < 1:
        raise RegisterLayoutDefinitionError("bit_count must be at least one.")
    if field.start_bit > 15 or field.start_bit + field.bit_count > 16:
        raise RegisterLayoutDefinitionError(
            f"BitField {field.name!r} does not fit in one 16-bit register."
        )
    if not isinstance(field.signed, bool):
        raise RegisterLayoutDefinitionError("signed must be a bool.")


class RegisterLayout:
    """Decode and update holding registers according to named field rules."""

    def __init__(
        self,
        fields: Sequence[RegisterField],
        *,
        register_count: int | None = None,
    ) -> None:
        """Initialize and validate the declarative layout."""
        if not isinstance(fields, Sequence) or isinstance(fields, str):
            raise RegisterLayoutDefinitionError("fields must be a sequence.")
        if isinstance(register_count, bool) or (
            register_count is not None and not isinstance(register_count, int)
        ):
            raise RegisterLayoutDefinitionError("register_count must be an int.")
        if register_count is not None and (
            register_count < 0 or register_count > MAX_HOLDING_REGISTERS
        ):
            raise RegisterLayoutDefinitionError(
                f"register_count must be in range 0..{MAX_HOLDING_REGISTERS}."
            )

        self.fields = tuple(fields)
        self._fields_by_name: dict[str, RegisterField] = {}
        masks: dict[int, int] = {}
        required_count = 0

        for field in self.fields:
            field_masks, field_end = self._build_field_span(field)
            required_count = max(required_count, field_end)

            if field.name in self._fields_by_name:
                raise RegisterLayoutDefinitionError(
                    f"Duplicate field name {field.name!r}."
                )

            for register, mask in field_masks.items():
                existing_mask = masks.get(register, 0)
                if existing_mask & mask:
                    raise RegisterLayoutDefinitionError(
                        f"Field {field.name!r} overlaps another field at register "
                        f"{register}."
                    )
                masks[register] = existing_mask | mask

            self._fields_by_name[field.name] = field

        if register_count is None:
            register_count = required_count
        elif register_count < required_count:
            raise RegisterLayoutDefinitionError(
                f"register_count {register_count} is smaller than required "
                f"{required_count}."
            )
        self.register_count = register_count

    @staticmethod
    def _build_field_span(field: RegisterField) -> tuple[dict[int, int], int]:
        if isinstance(field, Field):
            _validate_field(field)
            field_count = field.datatype.word_count * field.array_size
            field_end = field.offset + field_count
            if field_end > MAX_HOLDING_REGISTERS:
                raise RegisterLayoutDefinitionError(
                    f"Field {field.name!r} extends beyond the holding register "
                    "address range."
                )
            masks = {
                register: 0xFFFF
                for register in range(field.offset, field_end)
            }
            return masks, field_end

        if isinstance(field, BitField):
            _validate_bit_field(field)
            mask = ((1 << field.bit_count) - 1) << field.start_bit
            return {field.register: mask}, field.register + 1

        raise RegisterLayoutDefinitionError(
            "fields must contain Field or BitField instances."
        )

    def decode(self, registers: Sequence[int]) -> dict[str, Any]:
        """Decode all named fields from a holding register array."""
        self._validate_registers(registers)
        return {field.name: self._decode_field(field, registers) for field in self.fields}

    def update(
        self,
        registers: Sequence[int],
        values: Mapping[str, Any] | None = None,
        **keyword_values: Any,
    ) -> list[int]:
        """Return a copy with selected fields updated, preserving all other bits."""
        self._validate_registers(registers)
        updates = self._combine_updates(values, keyword_values)
        updated = list(registers)

        for name, value in updates.items():
            field = self._fields_by_name[name]
            if isinstance(field, Field):
                words = self._encode_numeric_field(field, value)
                for index, word in enumerate(words, start=field.offset):
                    updated[index] = word
            else:
                word = self._encode_bit_field(field, value)
                mask = ((1 << field.bit_count) - 1) << field.start_bit
                updated[field.register] = (
                    updated[field.register] & ~mask
                ) | (word & mask)

        return updated

    def _combine_updates(
        self,
        values: Mapping[str, Any] | None,
        keyword_values: Mapping[str, Any],
    ) -> dict[str, Any]:
        if values is not None and not isinstance(values, Mapping):
            raise RegisterLayoutValueError("values must be a mapping.")
        combined = dict(values or {})
        for name, value in keyword_values.items():
            if name in combined:
                raise RegisterLayoutValueError(f"Field {name!r} supplied twice.")
            combined[name] = value
        for name in combined:
            if name not in self._fields_by_name:
                raise RegisterLayoutValueError(f"Unknown field {name!r}.")
        return combined

    def _validate_registers(self, registers: Sequence[int]) -> None:
        if not isinstance(registers, Sequence) or isinstance(registers, (str, bytes)):
            raise RegisterLayoutValueError("registers must be a sequence.")
        if len(registers) < self.register_count:
            raise RegisterLayoutValueError(
                f"Expected at least {self.register_count} registers, got "
                f"{len(registers)}."
            )
        for index, register in enumerate(registers):
            if isinstance(register, bool) or not isinstance(register, int):
                raise RegisterLayoutValueError(f"Register {index} must be an int.")
            if not 0 <= register <= 0xFFFF:
                raise RegisterLayoutValueError(
                    f"Register {index} must be in range 0..65535."
                )

    def _decode_field(self, field: RegisterField, registers: Sequence[int]) -> Any:
        if isinstance(field, BitField):
            return self._decode_bit_field(field, registers[field.register])

        values = []
        for element in range(field.array_size):
            start = field.offset + element * field.datatype.word_count
            stop = start + field.datatype.word_count
            raw = self._words_to_bytes(registers[start:stop], field)
            value = struct.unpack(f">{field.datatype.struct_format}", raw)[0]
            if (
                field.datatype.is_float
                and field.nan_policy == NaNPolicy.REJECT
                and math.isnan(value)
            ):
                raise RegisterLayoutNaNError(
                    f"Field {field.name!r} contains NaN and nan_policy is REJECT."
                )
            values.append(value)
        return values[0] if field.array_size == 1 else values

    @staticmethod
    def _decode_bit_field(field: BitField, register: int) -> int | bool:
        raw = (register >> field.start_bit) & ((1 << field.bit_count) - 1)
        if not field.signed:
            if field.bit_count == 1:
                return bool(raw)
            return raw

        sign_bit = 1 << (field.bit_count - 1)
        return raw - (1 << field.bit_count) if raw & sign_bit else raw

    def _encode_numeric_field(self, field: Field, value: Any) -> list[int]:
        if field.array_size == 1:
            elements = [value]
        else:
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise RegisterLayoutValueError(
                    f"Field {field.name!r} requires a sequence of "
                    f"{field.array_size} values."
                )
            elements = list(value)
            if len(elements) != field.array_size:
                raise RegisterLayoutValueError(
                    f"Field {field.name!r} requires {field.array_size} values, got "
                    f"{len(elements)}."
                )

        words: list[int] = []
        for element in elements:
            raw = self._pack_numeric_value(field, element)
            words.extend(self._bytes_to_words(raw, field))
        return words

    @staticmethod
    def _pack_numeric_value(field: Field, value: Any) -> bytes:
        if field.datatype.is_float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RegisterLayoutValueError(
                    f"Field {field.name!r} requires an int or float."
                )
            if field.nan_policy == NaNPolicy.REJECT and math.isnan(value):
                raise RegisterLayoutNaNError(
                    f"Field {field.name!r} cannot be NaN when nan_policy is REJECT."
                )
            try:
                return struct.pack(f">{field.datatype.struct_format}", float(value))
            except OverflowError as exc:
                raise RegisterLayoutOverflowError(
                    f"Value {value!r} does not fit in {field.datatype.name}."
                ) from exc
            except struct.error as exc:
                raise RegisterLayoutValueError(
                    f"Value {value!r} is not a valid {field.datatype.name} value."
                ) from exc

        if isinstance(value, bool) or not isinstance(value, int):
            raise RegisterLayoutValueError(f"Field {field.name!r} requires an int.")
        signed = field.datatype.struct_format.islower()
        bits = field.datatype.word_count * 16
        minimum = -(1 << (bits - 1)) if signed else 0
        maximum = (1 << (bits - 1)) - 1 if signed else (1 << bits) - 1
        if not minimum <= value <= maximum:
            raise RegisterLayoutOverflowError(
                f"Value {value} does not fit in {field.datatype.name} "
                f"({minimum}..{maximum})."
            )
        return struct.pack(f">{field.datatype.struct_format}", value)

    @staticmethod
    def _encode_bit_field(field: BitField, value: Any) -> int:
        if not field.signed and field.bit_count == 1:
            if not isinstance(value, bool) and value not in (0, 1):
                raise RegisterLayoutValueError(
                    f"Field {field.name!r} requires bool, 0, or 1."
                )
            raw = int(value)
        else:
            if isinstance(value, bool) or not isinstance(value, int):
                raise RegisterLayoutValueError(
                    f"Field {field.name!r} requires an int."
                )
            bits = field.bit_count
            minimum = -(1 << (bits - 1)) if field.signed else 0
            maximum = (1 << (bits - 1)) - 1 if field.signed else (1 << bits) - 1
            if not minimum <= value <= maximum:
                raise RegisterLayoutOverflowError(
                    f"Value {value} does not fit in field {field.name!r} "
                    f"({minimum}..{maximum})."
                )
            raw = value & ((1 << bits) - 1)
        return raw << field.start_bit

    @staticmethod
    def _words_to_bytes(words: Sequence[int], field: Field) -> bytes:
        word_bytes = [
            word.to_bytes(2, field.byte_order, signed=False) for word in words
        ]
        if field.word_order == "little":
            word_bytes.reverse()
        return b"".join(word_bytes)

    @staticmethod
    def _bytes_to_words(raw: bytes, field: Field) -> list[int]:
        words = [
            int.from_bytes(raw[index : index + 2], field.byte_order, signed=False)
            for index in range(0, len(raw), 2)
        ]
        if field.word_order == "little":
            words.reverse()
        return words
