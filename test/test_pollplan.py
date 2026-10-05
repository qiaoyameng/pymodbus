"""Test the polling plan builder and the in-memory executor."""

import pytest

from pymodbus.pollplan import (
    MemoryExecutor,
    PollPlanError,
    PollPlanExecutionError,
    Signal,
    build_poll_plan,
)


FC_HOLDING = 3
FC_INPUT = 4


class TestBuildPollPlan:
    """Test plan building."""

    def test_sparse_signals_merge_within_allowed_gap(self):
        """Signals with allowed holes merge into one request."""
        signals = [
            Signal("a", 1, FC_HOLDING, 0, width=2, max_gap=4),
            Signal("b", 1, FC_HOLDING, 5, width=1, max_gap=4),
            Signal("c", 1, FC_HOLDING, 9, width=2, max_gap=4),
        ]
        plan = build_poll_plan(signals)
        assert len(plan.requests) == 1
        request = plan.requests[0]
        assert (request.device_id, request.function_code) == (1, FC_HOLDING)
        assert (request.address, request.count) == (0, 11)
        offsets = {m.name: (m.offset, m.width) for m in plan.mappings}
        assert offsets == {"a": (0, 2), "b": (5, 1), "c": (9, 2)}

    def test_hole_not_allowed_splits_requests(self):
        """A hole larger than allowed (or forbidden) splits the plan."""
        signals = [
            Signal("a", 1, FC_HOLDING, 0, width=2, max_gap=2),
            Signal("b", 1, FC_HOLDING, 10, width=1, max_gap=2),
        ]
        plan = build_poll_plan(signals)
        assert len(plan.requests) == 2
        assert [(r.address, r.count) for r in plan.requests] == [(0, 2), (10, 1)]

    def test_hole_requires_both_neighbours_to_allow(self):
        """If one neighbour forbids the hole, no merge happens."""
        signals = [
            Signal("a", 1, FC_HOLDING, 0, width=1, max_gap=10),
            Signal("b", 1, FC_HOLDING, 3, width=1, max_gap=0),
        ]
        plan = build_poll_plan(signals)
        assert len(plan.requests) == 2

    def test_devices_and_function_codes_not_merged(self):
        """Same addresses on different devices/codes stay separate."""
        signals = [
            Signal("a", 1, FC_HOLDING, 0, width=1, max_gap=100),
            Signal("b", 2, FC_HOLDING, 1, width=1, max_gap=100),
            Signal("c", 1, FC_INPUT, 2, width=1, max_gap=100),
        ]
        plan = build_poll_plan(signals)
        assert len(plan.requests) == 3
        keys = {(r.device_id, r.function_code) for r in plan.requests}
        assert keys == {(1, FC_HOLDING), (2, FC_HOLDING), (1, FC_INPUT)}

    def test_duplicate_signals_share_read(self):
        """Identical signals produce a single request and two mappings."""
        signals = [
            Signal("a", 1, FC_HOLDING, 4, width=2),
            Signal("b", 1, FC_HOLDING, 4, width=2),
        ]
        plan = build_poll_plan(signals)
        assert len(plan.requests) == 1
        assert plan.requests[0].count == 2
        assert [(m.name, m.offset) for m in plan.mappings] == [("a", 0), ("b", 0)]

    def test_overlapping_wide_signals_share_read(self):
        """Overlapping multi-register signals share one request."""
        signals = [
            Signal("a", 1, FC_HOLDING, 0, width=4),
            Signal("b", 1, FC_HOLDING, 2, width=4),
            Signal("c", 1, FC_HOLDING, 3, width=2),
        ]
        plan = build_poll_plan(signals)
        assert len(plan.requests) == 1
        assert (plan.requests[0].address, plan.requests[0].count) == (0, 6)
        offsets = {m.name: (m.offset, m.width) for m in plan.mappings}
        assert offsets == {"a": (0, 4), "b": (2, 4), "c": (3, 2)}

    def test_request_count_limit_splits_groups(self):
        """Groups never exceed the single request limit."""
        signals = [
            Signal("a", 1, FC_HOLDING, 0, width=3, max_gap=100),
            Signal("b", 1, FC_HOLDING, 3, width=3, max_gap=100),
            Signal("c", 1, FC_HOLDING, 6, width=3, max_gap=100),
        ]
        plan = build_poll_plan(signals, max_count=6)
        assert len(plan.requests) == 2
        assert all(r.count <= 6 for r in plan.requests)
        assert sorted((r.address, r.count) for r in plan.requests) == [(0, 3), (3, 6)]

    def test_signal_wider_than_limit_names_signal(self):
        """A signal that can never fit is reported by name."""
        signals = [
            Signal("ok", 1, FC_HOLDING, 0, width=2),
            Signal("too_wide", 1, FC_HOLDING, 10, width=6),
        ]
        with pytest.raises(PollPlanError, match="too_wide"):
            build_poll_plan(signals, max_count=4)

    def test_invalid_signal_parameters_name_signal(self):
        """Invalid width/address/gap are reported by signal name."""
        with pytest.raises(PollPlanError, match="bad_width"):
            build_poll_plan([Signal("bad_width", 1, FC_HOLDING, 0, width=0)])
        with pytest.raises(PollPlanError, match="bad_addr"):
            build_poll_plan([Signal("bad_addr", 1, FC_HOLDING, -1)])
        with pytest.raises(PollPlanError, match="bad_gap"):
            build_poll_plan([Signal("bad_gap", 1, FC_HOLDING, 0, max_gap=-1)])
        with pytest.raises(PollPlanError, match="dup"):
            build_poll_plan(
                [
                    Signal("dup", 1, FC_HOLDING, 0),
                    Signal("dup", 1, FC_HOLDING, 1),
                ]
            )

    def test_fewer_requests_beat_fewer_extra_registers(self):
        """Request count is minimized before extra registers."""
        signals = [
            Signal("a", 1, FC_HOLDING, 0, width=1, max_gap=100),
            Signal("b", 1, FC_HOLDING, 8, width=1, max_gap=100),
            Signal("c", 1, FC_HOLDING, 9, width=1, max_gap=100),
            Signal("d", 1, FC_HOLDING, 10, width=1, max_gap=100),
        ]
        plan = build_poll_plan(signals, max_count=11)
        # One request of 11 registers beats two requests.
        assert len(plan.requests) == 1
        assert plan.requests[0].count == 11

    def test_extra_registers_minimized_for_same_request_count(self):
        """Among plans with minimal requests, padding is minimized."""
        signals = [
            Signal("a", 1, FC_HOLDING, 0, width=1, max_gap=100),
            Signal("b", 1, FC_HOLDING, 8, width=1, max_gap=100),
            Signal("c", 1, FC_HOLDING, 9, width=1, max_gap=100),
            Signal("d", 1, FC_HOLDING, 10, width=1, max_gap=100),
        ]
        plan = build_poll_plan(signals, max_count=10)
        # Two requests are needed; grouping b+c+d reads 1 padding
        # register instead of grouping a+b (7 padding registers).
        assert [(r.address, r.count) for r in plan.requests] == [(0, 1), (8, 3)]

    def test_mapping_preserves_input_order(self):
        """Mappings follow the caller's signal order, not address order."""
        signals = [
            Signal("z", 1, FC_HOLDING, 10, width=1),
            Signal("y", 1, FC_HOLDING, 0, width=1),
        ]
        plan = build_poll_plan(signals)
        assert [m.name for m in plan.mappings] == ["z", "y"]


class TestMemoryExecutor:
    """Test the in-memory executor."""

    def test_run_returns_signals_in_order_with_values(self):
        """Decoded signals keep input order and expected values."""
        signals = [
            Signal("temp", 1, FC_HOLDING, 0, width=2, max_gap=2),
            Signal("pressure", 1, FC_HOLDING, 4, width=1, max_gap=2),
            Signal("flow", 2, FC_INPUT, 0, width=2),
        ]
        plan = build_poll_plan(signals)
        executor = MemoryExecutor()
        executor.write(1, FC_HOLDING, 0, [100, 200])
        executor.write(1, FC_HOLDING, 2, [999, 998])  # padding inside the hole
        executor.write(1, FC_HOLDING, 4, 42)
        executor.write(2, FC_INPUT, 0, [7, 8])
        result = executor.run(plan)
        assert list(result) == ["temp", "pressure", "flow"]
        assert result == {
            "temp": (100, 200),
            "pressure": (42,),
            "flow": (7, 8),
        }

    def test_overlapping_signals_decode_shared_registers(self):
        """Overlapping signals decode from the same response registers."""
        signals = [
            Signal("a", 1, FC_HOLDING, 0, width=4),
            Signal("b", 1, FC_HOLDING, 2, width=4),
        ]
        plan = build_poll_plan(signals)
        executor = MemoryExecutor()
        executor.write(1, FC_HOLDING, 0, [1, 2, 3, 4, 5, 6])
        result = executor.run(plan)
        assert result == {"a": (1, 2, 3, 4), "b": (3, 4, 5, 6)}

    def test_unreadable_hole_raises(self):
        """Reading registers that were never written fails loudly."""
        signals = [
            Signal("a", 1, FC_HOLDING, 0, width=1, max_gap=5),
            Signal("b", 1, FC_HOLDING, 3, width=1, max_gap=5),
        ]
        plan = build_poll_plan(signals)
        assert len(plan.requests) == 1  # hole merged into the request
        executor = MemoryExecutor()
        executor.write(1, FC_HOLDING, 0, 1)
        executor.write(1, FC_HOLDING, 3, 4)
        with pytest.raises(PollPlanExecutionError, match="unreadable registers"):
            executor.run(plan)

    def test_forbidden_hole_stays_outside_requests(self):
        """A forbidden hole is never part of any request."""
        signals = [
            Signal("a", 1, FC_HOLDING, 0, width=1, max_gap=0),
            Signal("b", 1, FC_HOLDING, 5, width=1, max_gap=0),
        ]
        plan = build_poll_plan(signals)
        assert len(plan.requests) == 2
        executor = MemoryExecutor()
        executor.write(1, FC_HOLDING, 0, 11)
        executor.write(1, FC_HOLDING, 5, 55)
        # Addresses 1-4 stay unwritten and unread; the plan must not touch them.
        assert executor.run(plan) == {"a": (11,), "b": (55,)}

    def test_decode_response_length_mismatch(self):
        """Decoding fails on missing or short responses."""
        plan = build_poll_plan([Signal("a", 1, FC_HOLDING, 0, width=2)])
        with pytest.raises(PollPlanExecutionError, match="expected 1 responses"):
            plan.decode([])
        with pytest.raises(PollPlanExecutionError, match="too short"):
            plan.decode([[1]])
