"""Tests for declarative holding-register layouts."""

import math
import struct

import pytest

from pymodbus.register_layout import (
    BitField,
    Field,
    NaNPolicy,
    RegisterDataType,
    RegisterLayout,
    RegisterLayoutDefinitionError,
    RegisterLayoutNaNError,
    RegisterLayoutOverflowError,
    RegisterLayoutValueError,
)


def test_disjoint_bitfields_share_register_and_preserve_reserved_bits():
    layout = RegisterLayout(
        [
            BitField("enabled", register=0, start_bit=0),
            BitField("alarm", register=0, start_bit=1),
            BitField("mode", register=0, start_bit=2, bit_count=4, signed=True),
        ],
        register_count=1,
    )

    assert layout.decode([0xFFFF]) == {
        "enabled": True,
        "alarm": True,
        "mode": -1,
    }

    updated = layout.update(
        [0xFFFF],
        enabled=False,
        alarm=True,
        mode=-2,
    )

    assert updated == [0xFFFA]
    assert layout.decode(updated) == {
        "enabled": False,
        "alarm": True,
        "mode": -2,
    }


def test_overlapping_bitfields_are_rejected_at_layout_time():
    with pytest.raises(RegisterLayoutDefinitionError, match="overlaps"):
        RegisterLayout(
            [
                BitField("first", register=0, start_bit=2, bit_count=2),
                BitField("second", register=0, start_bit=3, bit_count=2),
            ]
        )


def test_numeric_field_cannot_overlap_bitfield():
    with pytest.raises(RegisterLayoutDefinitionError, match="overlaps"):
        RegisterLayout(
            [
                BitField("flag", register=0, start_bit=0),
                Field("value", 0, RegisterDataType.UINT16),
            ]
        )


def test_duplicate_field_names_are_rejected():
    with pytest.raises(RegisterLayoutDefinitionError, match="Duplicate"):
        RegisterLayout(
            [
                BitField("same", register=0, start_bit=0),
                BitField("same", register=1, start_bit=0),
            ]
        )


def test_bitfields_must_stay_inside_one_register():
    with pytest.raises(RegisterLayoutDefinitionError):
        BitField("wide", register=0, start_bit=14, bit_count=3)

    with pytest.raises(RegisterLayoutDefinitionError):
        RegisterLayout(
            [BitField("outside", register=65536, start_bit=0)]
        )


def test_all_integer_widths_decode_from_independent_raw_registers():
    layout = RegisterLayout(
        [
            Field("int16", 0, RegisterDataType.INT16),
            Field("uint16", 1, RegisterDataType.UINT16),
            Field("int32", 2, RegisterDataType.INT32),
            Field("uint32", 4, RegisterDataType.UINT32),
            Field("int64", 6, RegisterDataType.INT64),
            Field("uint64", 10, RegisterDataType.UINT64),
        ]
    )
    registers = [
        0x8000,
        0xFFFF,
        0x8000,
        0x0000,
        0xFFFF,
        0xFFFF,
        0x8000,
        0x0000,
        0x0000,
        0x0000,
        0xFFFF,
        0xFFFF,
        0xFFFF,
        0xFFFF,
    ]

    assert layout.decode(registers) == {
        "int16": -(2**15),
        "uint16": 2**16 - 1,
        "int32": -(2**31),
        "uint32": 2**32 - 1,
        "int64": -(2**63),
        "uint64": 2**64 - 1,
    }


def test_all_integer_widths_update_from_independent_packed_values():
    layout = RegisterLayout(
        [
            Field("int16", 0, RegisterDataType.INT16),
            Field("uint16", 1, RegisterDataType.UINT16),
            Field("int32", 2, RegisterDataType.INT32),
            Field("uint32", 4, RegisterDataType.UINT32),
            Field("int64", 6, RegisterDataType.INT64),
            Field("uint64", 10, RegisterDataType.UINT64),
        ]
    )
    expected = b"".join(
        [
            struct.pack(">h", -123),
            struct.pack(">H", 456),
            struct.pack(">i", -789012),
            struct.pack(">I", 3456789),
            struct.pack(">q", -9_007_199_254_740_993),
            struct.pack(">Q", 9_007_199_254_740_993),
        ]
    )
    expected_words = [
        int.from_bytes(expected[index : index + 2], "big")
        for index in range(0, len(expected), 2)
    ]

    updated = layout.update(
        [0] * 14,
        values={
            "int16": -123,
            "uint16": 456,
            "int32": -789012,
            "uint32": 3456789,
            "int64": -9_007_199_254_740_993,
            "uint64": 9_007_199_254_740_993,
        },
    )

    assert updated == expected_words


@pytest.mark.parametrize(
    ("byte_order", "word_order", "registers"),
    [
        ("big", "big", [0x1234, 0x5678]),
        ("little", "big", [0x3412, 0x7856]),
        ("big", "little", [0x5678, 0x1234]),
        ("little", "little", [0x7856, 0x3412]),
    ],
)
def test_uint32_byte_and_word_order(byte_order, word_order, registers):
    layout = RegisterLayout(
        [Field("value", 0, RegisterDataType.UINT32, byte_order, word_order)]
    )

    assert layout.decode(registers)["value"] == 0x12345678


def test_word_swapped_float32_vector_uses_independent_wire_values():
    packed = struct.pack(">ff", 12.5, -3.25)
    canonical_words = [
        int.from_bytes(packed[index : index + 2], "big")
        for index in range(0, len(packed), 2)
    ]
    wire_words = [
        canonical_words[1],
        canonical_words[0],
        canonical_words[3],
        canonical_words[2],
    ]
    registers = [0xAAAA, *wire_words, 0xBBBB]
    layout = RegisterLayout(
        [
            Field(
                "temperatures",
                1,
                RegisterDataType.FLOAT32,
                word_order="little",
                array_size=2,
            )
        ],
        register_count=6,
    )

    assert layout.decode(registers)["temperatures"] == [12.5, -3.25]

    updated_packed = struct.pack(">ff", -6.0, 42.0)
    updated_canonical = [
        int.from_bytes(updated_packed[index : index + 2], "big")
        for index in range(0, len(updated_packed), 2)
    ]
    expected = [
        0xAAAA,
        updated_canonical[1],
        updated_canonical[0],
        updated_canonical[3],
        updated_canonical[2],
        0xBBBB,
    ]

    assert layout.update(registers, temperatures=[-6.0, 42.0]) == expected
    assert expected[0] == 0xAAAA
    assert expected[-1] == 0xBBBB


def test_word_swapped_float64_decode():
    packed = struct.pack(">d", 273.15)
    canonical_words = [
        int.from_bytes(packed[index : index + 2], "big")
        for index in range(0, len(packed), 2)
    ]
    wire_words = list(reversed(canonical_words))
    layout = RegisterLayout(
        [Field("temperature", 0, RegisterDataType.FLOAT64, word_order="little")]
    )

    assert layout.decode(wire_words)["temperature"] == 273.15


def test_word_swapped_float64_update_preserves_neighbor_registers():
    layout = RegisterLayout(
        [Field("temperature", 1, RegisterDataType.FLOAT64, word_order="little")],
        register_count=6,
    )
    packed = struct.pack(">d", 273.15)
    canonical_words = [
        int.from_bytes(packed[index : index + 2], "big")
        for index in range(0, len(packed), 2)
    ]

    updated = layout.update([0xAAAA] + [0] * 4 + [0xBBBB], temperature=273.15)

    assert updated == [0xAAAA, *reversed(canonical_words), 0xBBBB]


def test_partial_update_preserves_other_numeric_fields():
    layout = RegisterLayout(
        [
            Field("before", 0, RegisterDataType.UINT16),
            Field("middle", 1, RegisterDataType.INT16),
            Field("after", 2, RegisterDataType.UINT16),
        ]
    )

    assert layout.update([0xAAAA, 0xBBBB, 0xCCCC], middle=-1) == [
        0xAAAA,
        0xFFFF,
        0xCCCC,
    ]


def test_update_accepts_values_keyword_and_preserves_tail_registers():
    layout = RegisterLayout(
        [Field("value", 0, RegisterDataType.UINT16)],
        register_count=1,
    )

    assert layout.update([0, 0xCCCC], values={"value": 0x1234}) == [0x1234, 0xCCCC]


@pytest.mark.parametrize(
    ("datatype", "value"),
    [
        (RegisterDataType.INT16, 2**15),
        (RegisterDataType.UINT16, -1),
        (RegisterDataType.INT32, 2**31),
        (RegisterDataType.UINT32, 2**32),
        (RegisterDataType.INT64, 2**63),
        (RegisterDataType.UINT64, -1),
        (RegisterDataType.FLOAT32, 1e40),
    ],
)
def test_numeric_overflow_is_an_explicit_error(datatype, value):
    layout = RegisterLayout([Field("value", 0, datatype)])

    with pytest.raises(RegisterLayoutOverflowError):
        layout.update([0] * datatype.word_count, value=value)


def test_bitfield_overflow_and_invalid_flag_values_are_explicit_errors():
    layout = RegisterLayout(
        [
            BitField("flag", register=0, start_bit=0),
            BitField("mode", register=0, start_bit=2, bit_count=3, signed=True),
        ]
    )

    with pytest.raises(RegisterLayoutOverflowError):
        layout.update([0], mode=4)
    with pytest.raises(RegisterLayoutValueError):
        layout.update([0], flag=2)


def test_signed_single_bit_field_has_two_complement_values():
    layout = RegisterLayout([BitField("value", register=0, start_bit=3, signed=True)])

    assert layout.decode([0x0000])["value"] == 0
    assert layout.decode([0x0008])["value"] == -1
    assert layout.update([0xF000], value=-1) == [0xF008]


def test_vector_length_and_value_types_are_explicit_errors():
    layout = RegisterLayout(
        [Field("values", 0, RegisterDataType.FLOAT32, array_size=2)]
    )

    with pytest.raises(RegisterLayoutValueError):
        layout.update([0, 0], values=[1.0])
    with pytest.raises(RegisterLayoutValueError):
        layout.update([0, 0], values=["no", 1.0])


def test_nan_decode_and_update_policies_are_deterministic():
    nan_words = [0x7FC0, 0x0000]
    rejecting = RegisterLayout([Field("value", 0, RegisterDataType.FLOAT32)])
    allowing = RegisterLayout(
        [Field("value", 0, RegisterDataType.FLOAT32, nan_policy=NaNPolicy.ALLOW)]
    )

    with pytest.raises(RegisterLayoutNaNError):
        rejecting.decode(nan_words)
    with pytest.raises(RegisterLayoutNaNError):
        rejecting.update([0, 0], value=math.nan)

    decoded = allowing.decode(nan_words)["value"]
    assert math.isnan(decoded)
    assert allowing.update([0, 0], value=math.nan) == nan_words


def test_short_register_input_is_an_explicit_error():
    layout = RegisterLayout([Field("value", 1, RegisterDataType.UINT16)])

    with pytest.raises(RegisterLayoutValueError, match="Expected at least"):
        layout.decode([0])
    with pytest.raises(RegisterLayoutValueError, match="Expected at least"):
        layout.update([0], value=1)


def test_invalid_register_values_are_rejected():
    layout = RegisterLayout([Field("value", 0, RegisterDataType.UINT16)])

    with pytest.raises(RegisterLayoutValueError):
        layout.decode([0x10000])
    with pytest.raises(RegisterLayoutValueError):
        layout.decode([-1])


def test_unknown_and_duplicate_update_fields_are_rejected():
    layout = RegisterLayout([Field("value", 0, RegisterDataType.UINT16)])

    with pytest.raises(RegisterLayoutValueError, match="Unknown field"):
        layout.update([0], missing=1)
    with pytest.raises(RegisterLayoutValueError, match="supplied twice"):
        layout.update([0], {"value": 1}, value=2)


def test_register_count_smaller_than_fields_is_rejected():
    with pytest.raises(RegisterLayoutDefinitionError):
        RegisterLayout(
            [Field("value", 1, RegisterDataType.UINT16)], register_count=1
        )
