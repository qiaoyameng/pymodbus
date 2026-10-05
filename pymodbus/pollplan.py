"""Polling plan builder for sparse signals.

Devices often expose dozens of sparse signals. Reading them one by one
is slow, while reading one large block may cross forbidden addresses.
This module builds an optimized read plan from signal descriptions
(device id, function code, start address, width and the hole size that
may be merged around the signal) and decodes the responses back into
signals.

Within the protocol limit for a single request (``max_count``), the
planner first minimizes the number of requests and then the number of
extra (padding) registers read. Multi-register signals are never split
across requests. Duplicate or overlapping signals share the same read,
while signals on different devices or function codes are never merged.

A plan that cannot satisfy a signal raises :class:`PollPlanError`
naming that signal, instead of silently dropping it.

:class:`MemoryExecutor` is a pure in-memory executor that serves a
plan from a register store and decodes the responses, preserving
signal order and values.
"""

from __future__ import annotations

from dataclasses import dataclass, field


DEFAULT_MAX_COUNT = 125


class PollPlanError(Exception):
    """Raised when a signal cannot be satisfied by any plan."""


class PollPlanExecutionError(Exception):
    """Raised when a plan cannot be executed against a data source."""


@dataclass(frozen=True)
class Signal:
    """Description of a single signal to poll.

    :param name: unique signal name
    :param device_id: modbus device (slave) id
    :param function_code: modbus read function code
    :param address: first register address
    :param width: number of registers (>= 1), never split across requests
    :param max_gap: largest hole (in registers) next to this signal that
        may be included in a request in order to merge it with a
        neighbour; a hole is only merged when both neighbours allow it
    """

    name: str
    device_id: int
    function_code: int
    address: int
    width: int = 1
    max_gap: int = 0


@dataclass(frozen=True)
class ReadRequest:
    """A single read request of the plan."""

    device_id: int
    function_code: int
    address: int
    count: int


@dataclass(frozen=True)
class SignalMapping:
    """How to decode a signal from a request response.

    ``registers[offset : offset + width]`` of the response of
    ``requests[request_index]`` holds the signal value.
    """

    name: str
    request_index: int
    offset: int
    width: int


@dataclass
class PollPlan:
    """Request plan plus the mapping back to signals."""

    requests: list[ReadRequest] = field(default_factory=list)
    mappings: list[SignalMapping] = field(default_factory=list)

    def decode(self, responses: list[list[int]]) -> dict[str, tuple[int, ...]]:
        """Decode request responses into signal values, in signal order.

        :param responses: one register list per request, in plan order
        :raises PollPlanExecutionError: if a response is too short
        """
        if len(responses) != len(self.requests):
            raise PollPlanExecutionError(
                f"expected {len(self.requests)} responses, got {len(responses)}"
            )
        values: dict[str, tuple[int, ...]] = {}
        for mapping in self.mappings:
            response = responses[mapping.request_index]
            end = mapping.offset + mapping.width
            if end > len(response):
                raise PollPlanExecutionError(
                    f"response {mapping.request_index} too short for signal "
                    f"'{mapping.name}': need {end} registers, got {len(response)}"
                )
            values[mapping.name] = tuple(response[mapping.offset : end])
        return values


def _validate_signals(signals: list[Signal], max_count: int) -> None:
    """Check every signal can be satisfied, naming offenders."""
    names: set[str] = set()
    for signal in signals:
        if signal.name in names:
            raise PollPlanError(f"duplicate signal name '{signal.name}'")
        names.add(signal.name)
        if signal.address < 0:
            raise PollPlanError(
                f"signal '{signal.name}' has negative address {signal.address}"
            )
        if signal.width < 1:
            raise PollPlanError(
                f"signal '{signal.name}' has invalid width {signal.width}"
            )
        if signal.width > max_count:
            raise PollPlanError(
                f"signal '{signal.name}' needs {signal.width} registers, "
                f"exceeding the single request limit of {max_count}; "
                "multi-register signals cannot be split across requests"
            )
        if signal.max_gap < 0:
            raise PollPlanError(
                f"signal '{signal.name}' has negative max_gap {signal.max_gap}"
            )


def _group_intervals(group: list[Signal], max_count: int) -> list[list[Signal]]:
    """Partition sorted signals into request groups.

    Minimizes first the number of groups and then the number of extra
    registers read, using dynamic programming over the sorted signals.
    A group is feasible when its span fits ``max_count`` and every
    internal hole is allowed by both adjacent signals.
    """
    count = len(group)
    # span[i][j], union_len[i][j] and feasibility for signals i..j.
    span = [[0] * count for _ in range(count)]
    union_len = [[0] * count for _ in range(count)]
    feasible = [[False] * count for _ in range(count)]
    for i in range(count):
        end = group[i].address + group[i].width
        end_index = i
        covered = group[i].width
        hole_ok = True
        for j in range(i, count):
            if j > i:
                signal = group[j]
                hole = signal.address - end
                if hole > 0:
                    if hole > min(group[end_index].max_gap, signal.max_gap):
                        hole_ok = False
                    else:
                        covered += hole
                    end = signal.address + signal.width
                    end_index = j
                elif signal.address + signal.width > end:
                    covered += signal.address + signal.width - end
                    end = signal.address + signal.width
                    end_index = j
            span[i][j] = end - group[i].address
            union_len[i][j] = covered
            feasible[i][j] = hole_ok and span[i][j] <= max_count
    # best[j] = (requests, extra registers) for the first j signals.
    best = [(0, 0)] + [(count + 1, 0)] * count
    split = [-1] * (count + 1)
    for j in range(1, count + 1):
        for i in range(j):
            if not feasible[i][j - 1]:
                continue
            candidate = (
                best[i][0] + 1,
                best[i][1] + span[i][j - 1] - union_len[i][j - 1],
            )
            if candidate < best[j]:
                best[j] = candidate
                split[j] = i
    groups: list[list[Signal]] = []
    j = count
    while j > 0:
        i = split[j]
        groups.append(group[i:j])
        j = i
    groups.reverse()
    return groups


def build_poll_plan(
    signals: list[Signal], max_count: int = DEFAULT_MAX_COUNT
) -> PollPlan:
    """Build an optimized read plan for the given signals.

    :param signals: signals to poll, order is preserved in the mapping
    :param max_count: protocol limit of registers per single request
    :raises PollPlanError: naming the signal that cannot be satisfied
    """
    if max_count < 1:
        raise PollPlanError(f"max_count must be >= 1, got {max_count}")
    _validate_signals(signals, max_count)
    plan = PollPlan()
    keyed: dict[tuple[int, int], list[Signal]] = {}
    for signal in signals:
        keyed.setdefault((signal.device_id, signal.function_code), []).append(signal)
    for device_id, function_code in sorted(keyed):
        group = sorted(
            keyed[(device_id, function_code)],
            key=lambda sig: (sig.address, sig.width),
        )
        for request_signals in _group_intervals(group, max_count):
            address = request_signals[0].address
            end = max(sig.address + sig.width for sig in request_signals)
            request_index = len(plan.requests)
            plan.requests.append(
                ReadRequest(
                    device_id=device_id,
                    function_code=function_code,
                    address=address,
                    count=end - address,
                )
            )
            for signal in request_signals:
                plan.mappings.append(
                    SignalMapping(
                        name=signal.name,
                        request_index=request_index,
                        offset=signal.address - address,
                        width=signal.width,
                    )
                )
    # Restore the caller's signal order in the mapping.
    order = {signal.name: index for index, signal in enumerate(signals)}
    plan.mappings.sort(key=lambda mapping: order[mapping.name])
    return plan


class MemoryExecutor:
    """Pure in-memory executor serving read requests from a register store.

    Registers are written per (device id, function code) block. Reading
    an address that was never written raises
    :class:`PollPlanExecutionError`, modelling a forbidden/unreadable
    hole.
    """

    def __init__(self) -> None:
        """Initialize an empty register store."""
        self._store: dict[tuple[int, int], dict[int, int]] = {}

    def write(
        self,
        device_id: int,
        function_code: int,
        address: int,
        values: int | list[int] | tuple[int, ...],
    ) -> None:
        """Write one register or a sequence of registers."""
        if isinstance(values, int):
            values = [values]
        block = self._store.setdefault((device_id, function_code), {})
        for index, value in enumerate(values):
            block[address + index] = value

    def read(
        self, device_id: int, function_code: int, address: int, count: int
    ) -> list[int]:
        """Read ``count`` registers, failing on unreadable addresses."""
        block = self._store.get((device_id, function_code), {})
        missing = [
            addr for addr in range(address, address + count) if addr not in block
        ]
        if missing:
            raise PollPlanExecutionError(
                f"unreadable registers on device {device_id} "
                f"function {function_code}: addresses {missing}"
            )
        return [block[addr] for addr in range(address, address + count)]

    def run(self, plan: PollPlan) -> dict[str, tuple[int, ...]]:
        """Execute a plan and return decoded signals in signal order."""
        responses = [
            self.read(
                request.device_id,
                request.function_code,
                request.address,
                request.count,
            )
            for request in plan.requests
        ]
        return plan.decode(responses)
