"""Test the polling plan tool."""

import pytest

from pymodbus.pollplan import (
    MAX_READ_COUNT,
    MemoryPoller,
    PollPlan,
    PollPlanError,
    PollRequest,
    PollSignal,
    SignalMapping,
    build_plan,
)


def sig(name, address, width=1, max_gap=0, device_id=1, function_code=3):
    """Build a signal with compact defaults."""
    return PollSignal(
        name=name,
        device_id=device_id,
        function_code=function_code,
        address=address,
        width=width,
        max_gap=max_gap,
    )


class TestBuildPlan:
    """Test plan building."""

    def test_single_signal(self):
        """A single signal yields a single exact request."""
        plan = build_plan([sig("temp", 10, width=2)])
        assert plan.requests == (
            PollRequest(
                device_id=1,
                function_code=3,
                address=10,
                count=2,
                signals=(SignalMapping("temp", 0, 2),),
            ),
        )
        assert plan.signal_order == ("temp",)

    def test_merge_within_allowed_gap(self):
        """Signals separated by a readable hole share one request."""
        plan = build_plan(
            [
                sig("a", 0, max_gap=10),
                sig("b", 5, max_gap=10),
                sig("c", 8, width=2, max_gap=10),
            ]
        )
        assert len(plan.requests) == 1
        req = plan.requests[0]
        assert (req.address, req.count) == (0, 10)
        assert req.signals == (
            SignalMapping("a", 0, 1),
            SignalMapping("b", 5, 1),
            SignalMapping("c", 8, 2),
        )

    def test_no_merge_without_gap_allowance(self):
        """Without max_gap, adjacent-but-separate signals stay separate."""
        plan = build_plan([sig("a", 0), sig("b", 5)])
        assert len(plan.requests) == 2
        assert [r.address for r in plan.requests] == [0, 5]
        assert all(r.count == 1 for r in plan.requests)

    def test_both_neighbours_must_allow_hole(self):
        """A hole is only crossed when both neighbours allow it."""
        plan = build_plan([sig("a", 0, max_gap=10), sig("b", 5, max_gap=0)])
        assert len(plan.requests) == 2

    def test_duplicate_signals_share_read(self):
        """Duplicate signals map to the same offset in one request."""
        plan = build_plan([sig("a", 4, width=2), sig("b", 4, width=2)])
        assert len(plan.requests) == 1
        req = plan.requests[0]
        assert req.count == 2
        assert req.signals == (
            SignalMapping("a", 0, 2),
            SignalMapping("b", 0, 2),
        )

    def test_overlapping_wide_signals_share_read(self):
        """Overlapping multi-register signals share one request."""
        plan = build_plan(
            [
                sig("float_a", 100, width=4),
                sig("word_b", 101, width=2),
                sig("float_c", 103, width=2),
            ]
        )
        assert len(plan.requests) == 1
        req = plan.requests[0]
        assert (req.address, req.count) == (100, 5)
        assert req.signals == (
            SignalMapping("float_a", 0, 4),
            SignalMapping("word_b", 1, 2),
            SignalMapping("float_c", 3, 2),
        )

    def test_contained_signal_shares_read(self):
        """A signal fully contained in a wider one adds no extra read."""
        plan = build_plan([sig("wide", 10, width=8), sig("inner", 12, width=2)])
        assert len(plan.requests) == 1
        assert plan.requests[0].count == 8

    def test_devices_not_merged(self):
        """Signals on different devices are never merged."""
        plan = build_plan(
            [sig("a", 0, device_id=1), sig("b", 1, device_id=2)],
        )
        assert len(plan.requests) == 2
        assert {r.device_id for r in plan.requests} == {1, 2}

    def test_function_codes_not_merged(self):
        """Signals with different function codes are never merged."""
        plan = build_plan(
            [
                sig("hold", 0, function_code=3),
                sig("inp", 0, function_code=4),
                sig("coil", 0, function_code=1),
            ]
        )
        assert len(plan.requests) == 3
        assert {r.function_code for r in plan.requests} == {1, 3, 4}

    def test_split_at_protocol_limit(self):
        """Clusters never exceed the protocol count limit."""
        limit = MAX_READ_COUNT[3]
        plan = build_plan(
            [
                sig("a", 0, width=100, max_gap=10),
                sig("b", 100, width=100, max_gap=10),
            ]
        )
        assert len(plan.requests) == 2
        assert all(r.count <= limit for r in plan.requests)
        assert [(r.address, r.count) for r in plan.requests] == [(0, 100), (100, 100)]

    def test_wide_signal_not_split(self):
        """A wide signal near the limit moves as a whole to a new request."""
        plan = build_plan(
            [
                sig("a", 0, width=120, max_gap=10),
                sig("b", 120, width=10, max_gap=10),
            ]
        )
        assert [(r.address, r.count) for r in plan.requests] == [(0, 120), (120, 10)]
        assert plan.requests[1].signals == (SignalMapping("b", 0, 10),)

    def test_custom_max_count(self):
        """A custom protocol limit is honoured."""
        plan = build_plan(
            [sig("a", 0, width=3, max_gap=2), sig("b", 3, width=3, max_gap=2)],
            max_count={3: 4},
        )
        assert len(plan.requests) == 2
        assert all(r.count <= 4 for r in plan.requests)

    def test_signal_wider_than_limit_named_in_error(self):
        """A signal wider than the protocol limit is reported by name."""
        with pytest.raises(PollPlanError, match="too_wide"):
            build_plan([sig("ok", 0), sig("too_wide", 10, width=MAX_READ_COUNT[3] + 1)])

    def test_invalid_signals_named_in_error(self):
        """Invalid signal definitions are reported by name."""
        with pytest.raises(PollPlanError, match="bad_fc"):
            build_plan([sig("bad_fc", 0, function_code=6)])
        with pytest.raises(PollPlanError, match="zero_width"):
            build_plan([sig("zero_width", 0, width=0)])
        with pytest.raises(PollPlanError, match="neg_addr"):
            build_plan([sig("neg_addr", -1)])
        with pytest.raises(PollPlanError, match="neg_gap"):
            build_plan([sig("neg_gap", 0, max_gap=-1)])

    def test_request_count_minimized_before_extra_registers(self):
        """Merging prefers fewer requests, holes only when allowed."""
        plan = build_plan(
            [
                sig("a", 0, max_gap=3),
                sig("b", 3, max_gap=3),
                sig("c", 6, max_gap=3),
            ]
        )
        assert len(plan.requests) == 1
        assert plan.requests[0].count == 7


class TestDecode:
    """Test response decoding."""

    def test_decode_mapping(self):
        """Response values are sliced per the decode mapping."""
        req = PollRequest(
            device_id=1,
            function_code=3,
            address=0,
            count=6,
            signals=(SignalMapping("a", 0, 1), SignalMapping("c", 4, 2)),
        )
        assert PollPlan.decode(req, [10, 1, 2, 3, 40, 41]) == {
            "a": [10],
            "c": [40, 41],
        }

    def test_decode_length_mismatch(self):
        """A short/long response is rejected."""
        req = PollRequest(
            device_id=1,
            function_code=3,
            address=0,
            count=2,
            signals=(SignalMapping("a", 0, 2),),
        )
        with pytest.raises(PollPlanError, match="expected 2"):
            PollPlan.decode(req, [1])


class TestMemoryPoller:
    """Test the in-memory executor."""

    def test_order_and_values(self):
        """Decoded signals appear in declaration order with their values."""
        signals = [
            sig("z_last", 20, width=2),
            sig("a_first", 0, max_gap=10),
            sig("m_mid", 5, max_gap=10),
        ]
        plan = build_plan(signals)
        memory = {
            (1, 3): {0: 7, 1: 0, 2: 0, 3: 0, 4: 0, 5: 42, 20: 1, 21: 2},
        }
        result = MemoryPoller(plan, memory).to_dict()
        assert list(result) == ["z_last", "a_first", "m_mid"]
        assert result == {"z_last": [1, 2], "a_first": [7], "m_mid": [42]}

    def test_merged_hole_values_ignored(self):
        """Hole registers inside a merged request are read but not decoded."""
        plan = build_plan([sig("a", 0, max_gap=4), sig("b", 4, max_gap=4)])
        assert len(plan.requests) == 1
        memory = {(1, 3): {0: 11, 1: 900, 2: 901, 3: 902, 4: 44}}
        assert MemoryPoller(plan, memory).to_dict() == {"a": [11], "b": [44]}

    def test_unreadable_hole_not_crossed(self):
        """A hole beyond max_gap is never part of a request."""
        plan = build_plan([sig("a", 0, width=2), sig("b", 10)])
        assert len(plan.requests) == 2
        memory = {(1, 3): {0: 1, 1: 2, 10: 3}}
        assert MemoryPoller(plan, memory).to_dict() == {"a": [1, 2], "b": [3]}

    def test_unreadable_address_raises(self):
        """Reading an address absent from memory raises instead of guessing."""
        plan = build_plan([sig("a", 0, max_gap=10), sig("b", 5, max_gap=10)])
        assert len(plan.requests) == 1
        memory = {(1, 3): {0: 1, 5: 2}}  # hole 1..4 is forbidden
        with pytest.raises(PollPlanError, match="not readable"):
            MemoryPoller(plan, memory).to_dict()

    def test_overlapping_wide_signals_decoded(self):
        """Overlapping wide signals decode from the shared read."""
        plan = build_plan(
            [sig("float_a", 100, width=4), sig("word_b", 101, width=2)]
        )
        memory = {(1, 3): {100: 1, 101: 2, 102: 3, 103: 4}}
        assert MemoryPoller(plan, memory).to_dict() == {
            "float_a": [1, 2, 3, 4],
            "word_b": [2, 3],
        }

    def test_multiple_devices_and_functions(self):
        """Each device/function group reads from its own memory block."""
        plan = build_plan(
            [
                sig("hold", 0, device_id=1, function_code=3),
                sig("inp", 0, device_id=1, function_code=4),
                sig("other_dev", 0, device_id=2, function_code=3),
            ]
        )
        memory = {
            (1, 3): {0: 30},
            (1, 4): {0: 40},
            (2, 3): {0: 20},
        }
        assert MemoryPoller(plan, memory).to_dict() == {
            "hold": [30],
            "inp": [40],
            "other_dev": [20],
        }

    def test_duplicate_signals_both_decoded(self):
        """Duplicate names sharing a read each get their values."""
        plan = build_plan([sig("a", 4, width=2), sig("b", 4, width=2)])
        memory = {(1, 3): {4: 8, 5: 9}}
        assert MemoryPoller(plan, memory).to_dict() == {"a": [8, 9], "b": [8, 9]}
