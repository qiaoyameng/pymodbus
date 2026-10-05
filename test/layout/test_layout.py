"""Test declarative register layouts."""

import math

import pytest

from pymodbus.layout import (
    DecodeError,
    EncodeError,
    FieldDef,
    FieldType,
    LayoutError,
    RegisterLayout,
)


class TestLayoutValidation:
    """Layout-time validation of field definitions."""

    def test_scalar_overlap_rejected(self):
        """Overlapping scalar fields must be rejected."""
        with pytest.raises(LayoutError):
            RegisterLayout(
                [
                    FieldDef("a", 0, FieldType.INT32),
                    FieldDef("b", 1, FieldType.UINT16),
                ]
            )

    def test_scalar_overlap_multiregister(self):
        """A 64 bit field overlapping a later field must be rejected."""
        with pytest.raises(LayoutError):
            RegisterLayout(
                [
                    FieldDef("a", 0, FieldType.INT64),
                    FieldDef("b", 3, FieldType.FLOAT32),
                ]
            )

    def test_scalar_over_bitfield_register_rejected(self):
        """A scalar may not sit on a register holding bit fields."""
        with pytest.raises(LayoutError):
            RegisterLayout(
                [
                    FieldDef("flag", 0, FieldType.BITS, bit=0),
                    FieldDef("a", 0, FieldType.UINT16),
                ]
            )

    def test_overlapping_bitfields_rejected(self):
        """Bit fields sharing bits in one register must be rejected."""
        with pytest.raises(LayoutError):
            RegisterLayout(
                [
                    FieldDef("a", 0, FieldType.BITS, bit=0, width=4),
                    FieldDef("b", 0, FieldType.BITS, bit=3, width=4),
                ]
            )

    def test_disjoint_bitfields_accepted(self):
        """Disjoint bit fields may share the same register."""
        layout = RegisterLayout(
            [
                FieldDef("flag_a", 0, FieldType.BITS, bit=0),
                FieldDef("flag_b", 0, FieldType.BITS, bit=1),
                FieldDef("level", 0, FieldType.BITS, bit=4, width=4, signed=True),
            ]
        )
        assert layout.span == 1
        assert layout.fields == ("flag_a", "flag_b", "level")

    def test_duplicate_name_rejected(self):
        """Field names must be unique."""
        with pytest.raises(LayoutError):
            RegisterLayout(
                [
                    FieldDef("a", 0, FieldType.UINT16),
                    FieldDef("a", 1, FieldType.UINT16),
                ]
            )

    @pytest.mark.parametrize(
        "field",
        [
            FieldDef("a", -1, FieldType.UINT16),
            FieldDef("a", 0, FieldType.BITS),
            FieldDef("a", 0, FieldType.BITS, bit=16),
            FieldDef("a", 0, FieldType.BITS, bit=-1),
            FieldDef("a", 0, FieldType.BITS, bit=0, width=0),
            FieldDef("a", 0, FieldType.BITS, bit=0, width=17),
            FieldDef("a", 0, FieldType.BITS, bit=14, width=4),
            FieldDef("a", 0, FieldType.BITS, bit=0, width=1, signed=True),
            FieldDef("a", 0, FieldType.BITS, bit=0, byte_order="little"),
            FieldDef("a", 0, FieldType.BITS, bit=0, word_order="little"),
            FieldDef("a", 0, FieldType.UINT16, bit=3),
            FieldDef("a", 0, FieldType.UINT16, byte_order="middle"),
            FieldDef("a", 0, FieldType.UINT16, word_order="middle"),
        ],
    )
    def test_illegal_definitions_rejected(self, field):
        """Illegal offsets, bit ranges and endianness must be rejected."""
        with pytest.raises(LayoutError):
            RegisterLayout([field])

    def test_illegal_nan_policy_rejected(self):
        """Unknown NaN policies must be rejected."""
        with pytest.raises(LayoutError):
            RegisterLayout(
                [FieldDef("a", 0, FieldType.FLOAT32)], nan_policy="ignore"
            )


class TestScalarDecode:
    """Decode scalars from hand-computed register patterns."""

    def test_int16_negative(self):
        """0xFF9C is -100 as signed 16 bit."""
        layout = RegisterLayout([FieldDef("temp", 0, FieldType.INT16)])
        assert layout.decode([0xFF9C]) == {"temp": -100}

    def test_uint16(self):
        layout = RegisterLayout([FieldDef("count", 0, FieldType.UINT16)])
        assert layout.decode([0xFF9C]) == {"count": 65436}

    def test_int32_big_word_order(self):
        layout = RegisterLayout([FieldDef("v", 0, FieldType.INT32)])
        assert layout.decode([0xFFFF, 0xFF9C]) == {"v": -100}

    def test_uint32_little_word_order(self):
        """Swapped word order: least significant register first."""
        layout = RegisterLayout(
            [FieldDef("v", 0, FieldType.UINT32, word_order="little")]
        )
        assert layout.decode([0x5678, 0x1234]) == {"v": 0x12345678}

    def test_int64(self):
        layout = RegisterLayout([FieldDef("v", 0, FieldType.INT64)])
        assert layout.decode([0xFFFF, 0xFFFF, 0xFFFF, 0xFF9C]) == {"v": -100}

    def test_uint64_little_word_order(self):
        layout = RegisterLayout(
            [FieldDef("v", 0, FieldType.UINT64, word_order="little")]
        )
        regs = [0xBC9A, 0x5678, 0x1234, 0xF0DE]
        assert layout.decode(regs) == {"v": 0xF0DE12345678BC9A}

    def test_byte_order_little(self):
        """Byte swap inside each register."""
        layout = RegisterLayout(
            [FieldDef("v", 0, FieldType.INT16, byte_order="little")]
        )
        assert layout.decode([0x9CFF]) == {"v": -100}

    def test_float32(self):
        """250.5 == 0x437A8000."""
        layout = RegisterLayout([FieldDef("p", 0, FieldType.FLOAT32)])
        assert layout.decode([0x437A, 0x8000]) == {"p": 250.5}

    def test_float64(self):
        """-1234.5 == 0xC0934A0000000000."""
        layout = RegisterLayout([FieldDef("p", 0, FieldType.FLOAT64)])
        regs = [0xC093, 0x4A00, 0x0000, 0x0000]
        assert layout.decode(regs) == {"p": -1234.5}

    def test_extra_registers_ignored(self):
        """Longer reads than the layout span are fine."""
        layout = RegisterLayout([FieldDef("v", 0, FieldType.UINT16)])
        assert layout.decode([7, 8, 9]) == {"v": 7}


class TestBitFields:
    """Two flags and a negative bit field sharing one register."""

    def make_layout(self):
        return RegisterLayout(
            [
                FieldDef("flag_a", 0, FieldType.BITS, bit=0),
                FieldDef("flag_b", 0, FieldType.BITS, bit=1),
                FieldDef("level", 0, FieldType.BITS, bit=4, width=4, signed=True),
            ]
        )

    def test_decode_masks(self):
        """0x00D3 = flags a/b set, level nibble 0b1101 == -3."""
        layout = self.make_layout()
        assert layout.decode([0x00D3]) == {
            "flag_a": True,
            "flag_b": True,
            "level": -3,
        }

    def test_decode_ignores_reserved_bits(self):
        """Reserved bits outside the fields must not leak into values."""
        layout = self.make_layout()
        assert layout.decode([0xFF0C]) == {
            "flag_a": False,
            "flag_b": False,
            "level": 0,
        }

    def test_encode_masks(self):
        """flag_a + level -2 must produce exactly 0x00E1 from zero."""
        layout = self.make_layout()
        assert layout.encode({"flag_a": True, "level": -2}) == [0x00E1]

    def test_partial_update_preserves_reserved_bits(self):
        """Unspecified fields and reserved bits survive a partial update."""
        layout = self.make_layout()
        base = [0x0B02]  # flag_b set, reserved bits 8/9/11 set
        assert layout.encode({"flag_a": True, "level": -2}, base=base) == [0x0BE3]

    def test_partial_update_clears_bit(self):
        """Explicitly clearing a flag must not touch the neighbours."""
        layout = self.make_layout()
        base = [0x00D3]  # flag_a, flag_b, level == -3
        assert layout.encode({"flag_b": False}, base=base) == [0x00D1]

    @pytest.mark.parametrize("value", [8, -9, 100])
    def test_signed_bitfield_overflow(self, value):
        """A signed 4 bit field only holds -8..7."""
        layout = self.make_layout()
        with pytest.raises(EncodeError):
            layout.encode({"level": value})

    def test_flag_rejects_non_binary(self):
        layout = self.make_layout()
        with pytest.raises(EncodeError):
            layout.encode({"flag_a": 2})


class TestFloatVectorSwappedWords:
    """Float32 vector with swapped word order, checked against constants."""

    def make_layout(self):
        return RegisterLayout(
            [
                FieldDef("v0", 0, FieldType.FLOAT32, word_order="little"),
                FieldDef("v1", 2, FieldType.FLOAT32, word_order="little"),
                FieldDef("v2", 4, FieldType.FLOAT32, word_order="little"),
            ]
        )

    # 1.0 == 0x3F800000, -2.5 == 0xC0200000, 250.5 == 0x437A8000
    EXPECTED_REGS = [0x0000, 0x3F80, 0x0000, 0xC020, 0x8000, 0x437A]

    def test_decode(self):
        layout = self.make_layout()
        assert layout.decode(self.EXPECTED_REGS) == {
            "v0": 1.0,
            "v1": -2.5,
            "v2": 250.5,
        }

    def test_encode(self):
        layout = self.make_layout()
        assert layout.encode({"v0": 1.0, "v1": -2.5, "v2": 250.5}) == (
            self.EXPECTED_REGS
        )

    def test_partial_encode_keeps_other_vector_elements(self):
        layout = self.make_layout()
        regs = layout.encode({"v1": -2.5}, base=self.EXPECTED_REGS)
        assert regs == self.EXPECTED_REGS


class TestPartialUpdate:
    """Partial updates across mixed scalar and bit fields."""

    def test_unspecified_scalars_preserved(self):
        layout = RegisterLayout(
            [
                FieldDef("temp", 0, FieldType.INT16),
                FieldDef("pressure", 1, FieldType.FLOAT32),
                FieldDef("mode", 3, FieldType.UINT16),
            ]
        )
        base = layout.encode({"temp": -100, "pressure": 250.5, "mode": 7})
        assert base == [0xFF9C, 0x437A, 0x8000, 0x0007]
        updated = layout.encode({"mode": 9}, base=base)
        assert updated == [0xFF9C, 0x437A, 0x8000, 0x0009]
        assert layout.decode(updated) == {"temp": -100, "pressure": 250.5, "mode": 9}

    def test_encode_without_base_zero_fills(self):
        layout = RegisterLayout(
            [
                FieldDef("a", 0, FieldType.UINT16),
                FieldDef("b", 2, FieldType.UINT16),
            ]
        )
        assert layout.encode({"b": 5}) == [0, 0, 5]


class TestDeterministicErrors:
    """Overflow, NaN policy and short input must raise defined errors."""

    @pytest.mark.parametrize(
        ("field_type", "value"),
        [
            (FieldType.UINT16, 65536),
            (FieldType.UINT16, -1),
            (FieldType.INT16, 32768),
            (FieldType.INT16, -32769),
            (FieldType.UINT32, 2**32),
            (FieldType.INT32, -(2**31) - 1),
            (FieldType.UINT64, 2**64),
            (FieldType.INT64, 2**63),
        ],
    )
    def test_int_overflow(self, field_type, value):
        layout = RegisterLayout([FieldDef("v", 0, field_type)])
        with pytest.raises(EncodeError):
            layout.encode({"v": value})

    def test_float32_overflow(self):
        layout = RegisterLayout([FieldDef("v", 0, FieldType.FLOAT32)])
        with pytest.raises(EncodeError):
            layout.encode({"v": 1e300})

    def test_wrong_value_type(self):
        layout = RegisterLayout([FieldDef("v", 0, FieldType.INT16)])
        with pytest.raises(EncodeError):
            layout.encode({"v": "x"})

    def test_nan_encode_rejected_by_default(self):
        layout = RegisterLayout([FieldDef("v", 0, FieldType.FLOAT32)])
        with pytest.raises(EncodeError):
            layout.encode({"v": float("nan")})

    def test_nan_encode_allowed(self):
        layout = RegisterLayout(
            [FieldDef("v", 0, FieldType.FLOAT32)], nan_policy="allow"
        )
        regs = layout.encode({"v": float("nan")})
        assert regs[0] & 0x7F80 == 0x7F80  # exponent all ones
        assert regs[0] & 0x007F or regs[1]  # mantissa non-zero

    def test_nan_decode_rejected_by_default(self):
        """0x7FC00000 is a quiet NaN."""
        layout = RegisterLayout([FieldDef("v", 0, FieldType.FLOAT32)])
        with pytest.raises(DecodeError):
            layout.decode([0x7FC0, 0x0000])

    def test_nan_decode_allowed(self):
        layout = RegisterLayout(
            [FieldDef("v", 0, FieldType.FLOAT32)], nan_policy="allow"
        )
        assert math.isnan(layout.decode([0x7FC0, 0x0000])["v"])

    def test_decode_short_input(self):
        layout = RegisterLayout([FieldDef("v", 0, FieldType.FLOAT64)])
        with pytest.raises(DecodeError):
            layout.decode([0x0000, 0x0000])

    def test_encode_short_base(self):
        layout = RegisterLayout([FieldDef("v", 0, FieldType.FLOAT64)])
        with pytest.raises(EncodeError):
            layout.encode({"v": 1.0}, base=[0x0000])

    def test_illegal_register_value_decode(self):
        layout = RegisterLayout([FieldDef("v", 0, FieldType.UINT16)])
        with pytest.raises(DecodeError):
            layout.decode([0x1_0000])

    def test_illegal_register_value_encode_base(self):
        layout = RegisterLayout([FieldDef("v", 0, FieldType.UINT16)])
        with pytest.raises(EncodeError):
            layout.encode({"v": 1}, base=[-1])

    def test_unknown_field_encode(self):
        layout = RegisterLayout([FieldDef("v", 0, FieldType.UINT16)])
        with pytest.raises(EncodeError):
            layout.encode({"nope": 1})
