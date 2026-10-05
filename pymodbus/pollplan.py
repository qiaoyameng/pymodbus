"""Polling plan tool for sparse modbus signals.

Devices often expose dozens of sparse signals. Reading them one by one
is slow, while reading one big block may cross forbidden addresses.
This module builds an optimized read plan from a list of signals and
decodes the responses back into per-signal values.

The planner groups signals by (device_id, function_code), merges
signals whose hole (gap) is readable, and emits requests that stay
within the protocol count limit:

- first priority: minimize the number of requests
- second priority: minimize the number of extra (hole) registers read
- a multi-register signal is never split across requests
- duplicate / overlapping signals share the same read
- signals on different devices or function codes are never merged

If no legal plan exists for a signal (e.g. it is wider than the
protocol limit), :class:`PollPlanError` names the offending signal
instead of silently dropping it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .exceptions import ModbusException


__all__ = [
    "MAX_READ_COUNT",
    "MemoryPoller",
    "PollPlan",
    "PollPlanError",
    "PollRequest",
    "PollSignal",
    "SignalMapping",
    "build_plan",
]

#: Protocol limit for a single read request, per function code.
MAX_READ_COUNT: dict[int, int] = {
    1: 2000,  # read coils
    2: 2000,  # read discrete inputs
    3: 125,  # read holding registers
    4: 125,  # read input registers
}


class PollPlanError(ModbusException):
    """Raised when no legal polling plan exists for a signal."""


@dataclass(frozen=True)
class PollSignal:
    """A single sparse signal to poll.

    :param name: unique identifier used in the decoded result
    :param device_id: modbus device id (slave)
    :param function_code: read function code (1, 2, 3 or 4)
    :param address: start address of the signal
    :param width: number of coils/registers the signal occupies
    :param max_gap: maximum hole (in coils/registers) after this signal
        that may be included in a request to merge with a neighbour.
        Holes larger than this are considered unreadable/forbidden and
        are never crossed.
    """

    name: str
    device_id: int
    function_code: int
    address: int
    width: int = 1
    max_gap: int = 0


@dataclass(frozen=True)
class SignalMapping:
    """Decode mapping from a request response back to a signal.

    :param name: signal name
    :param offset: offset of the signal within the response values
    :param width: number of values belonging to the signal
    """

    name: str
    offset: int
    width: int


@dataclass(frozen=True)
class PollRequest:
    """A single read request emitted by the planner.

    :param device_id: modbus device id (slave)
    :param function_code: read function code (1, 2, 3 or 4)
    :param address: start address of the request
    :param count: number of coils/registers to read
    :param signals: decode mapping, ordered by offset
    """

    device_id: int
    function_code: int
    address: int
    count: int
    signals: tuple[SignalMapping, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PollPlan:
    """A complete polling plan.

    :param requests: requests to execute, in execution order
    :param signal_order: signal names in declaration order
    """

    requests: tuple[PollRequest, ...]
    signal_order: tuple[str, ...]

    @staticmethod
    def decode(request: PollRequest, values: Sequence[int]) -> dict[str, list[int]]:
        """Decode response *values* of *request* into per-signal values.

        :param request: the request the values belong to
        :param values: response values, must have exactly ``request.count`` entries
        :raises PollPlanError: if the response length does not match the request
        """
        if len(values) != request.count:
            raise PollPlanError(
                f"Response for request {request!r} has {len(values)} values, "
                f"expected {request.count}"
            )
        result: dict[str, list[int]] = {}
        for mapping in request.signals:
            result[mapping.name] = list(values[mapping.offset : mapping.offset + mapping.width])
        return result


def _validate(signal: PollSignal, max_count: Mapping[int, int]) -> None:
    """Validate a single signal, naming it in any error."""
    if signal.function_code not in max_count:
        raise PollPlanError(
            f"Signal {signal.name!r}: unsupported function code "
            f"{signal.function_code}, expected one of {sorted(max_count)}"
        )
    if signal.address < 0:
        raise PollPlanError(f"Signal {signal.name!r}: negative address {signal.address}")
    if signal.width < 1:
        raise PollPlanError(f"Signal {signal.name!r}: width must be >= 1, got {signal.width}")
    if signal.max_gap < 0:
        raise PollPlanError(f"Signal {signal.name!r}: max_gap must be >= 0, got {signal.max_gap}")
    limit = max_count[signal.function_code]
    if signal.width > limit:
        raise PollPlanError(
            f"Signal {signal.name!r}: width {signal.width} exceeds the protocol "
            f"limit of {limit} for function code {signal.function_code} and "
            f"multi-register signals must not be split across requests"
        )


def _cluster_group(
    signals: list[PollSignal], limit: int
) -> list[list[PollSignal]]:
    """Greedily cluster one (device, function code) group.

    Signals are sorted by address; a signal joins the current cluster
    when the hole to the cluster end is readable (allowed by the
    ``max_gap`` of both neighbours) and the merged span stays within
    *limit*. Overlapping and duplicate signals always share a cluster.
    """
    ordered = sorted(signals, key=lambda s: (s.address, -s.width, s.name))
    clusters: list[list[PollSignal]] = []
    current: list[PollSignal] = []
    cluster_start = 0
    cluster_end = 0
    last_signal: PollSignal | None = None
    for signal in ordered:
        signal_end = signal.address + signal.width
        if not current:
            current = [signal]
            cluster_start = signal.address
            cluster_end = signal_end
            last_signal = signal
            continue
        hole = max(0, signal.address - cluster_end)
        allowed = min(last_signal.max_gap, signal.max_gap)  # type: ignore[union-attr]
        new_end = max(cluster_end, signal_end)
        if hole <= allowed and new_end - cluster_start <= limit:
            current.append(signal)
            cluster_end = new_end
        else:
            clusters.append(current)
            current = [signal]
            cluster_start = signal.address
            cluster_end = signal_end
        last_signal = signal
    if current:
        clusters.append(current)
    return clusters


def build_plan(
    signals: Sequence[PollSignal],
    max_count: Mapping[int, int] | None = None,
) -> PollPlan:
    """Build an optimized polling plan for *signals*.

    :param signals: signals to poll
    :param max_count: optional override of :data:`MAX_READ_COUNT`
    :raises PollPlanError: naming the signal that cannot be planned
    """
    limits = dict(MAX_READ_COUNT if max_count is None else max_count)
    for signal in signals:
        _validate(signal, limits)

    groups: dict[tuple[int, int], list[PollSignal]] = {}
    for signal in signals:
        groups.setdefault((signal.device_id, signal.function_code), []).append(signal)

    requests: list[PollRequest] = []
    for device_id, function_code in sorted(groups):
        group = groups[(device_id, function_code)]
        for cluster in _cluster_group(group, limits[function_code]):
            address = min(s.address for s in cluster)
            count = max(s.address + s.width for s in cluster) - address
            mappings = tuple(
                SignalMapping(name=s.name, offset=s.address - address, width=s.width)
                for s in sorted(cluster, key=lambda s: (s.address, s.name))
            )
            requests.append(
                PollRequest(
                    device_id=device_id,
                    function_code=function_code,
                    address=address,
                    count=count,
                    signals=mappings,
                )
            )
    return PollPlan(
        requests=tuple(requests),
        signal_order=tuple(s.name for s in signals),
    )


class MemoryPoller:
    """Pure in-memory executor for a :class:`PollPlan`.

    *memory* maps ``(device_id, function_code)`` to a sparse
    ``address -> value`` mapping. Any address a request touches that is
    absent from the memory raises :class:`PollPlanError`, which makes
    unreadable holes immediately visible instead of silently read.
    """

    def __init__(
        self,
        plan: PollPlan,
        memory: Mapping[tuple[int, int], Mapping[int, int]],
    ) -> None:
        """Initialize with a plan and the simulated device memory."""
        self.plan = plan
        self.memory = memory

    def to_dict(self) -> dict[str, list[int]]:
        """Execute the plan against memory and return decoded signal values.

        Signals appear in declaration order; each value list has exactly
        the signal's width entries, in address order.
        """
        decoded: dict[str, list[int]] = {}
        for request in self.plan.requests:
            block = self.memory.get((request.device_id, request.function_code), {})
            values: list[int] = []
            for offset in range(request.count):
                address = request.address + offset
                if address not in block:
                    raise PollPlanError(
                        f"Request {request!r} reads address {address} on device "
                        f"{request.device_id} function {request.function_code} "
                        f"which is not readable"
                    )
                values.append(block[address])
            decoded.update(PollPlan.decode(request, values))
        missing = [name for name in self.plan.signal_order if name not in decoded]
        if missing:
            raise PollPlanError(f"Signals not covered by any request: {missing}")
        return {name: decoded[name] for name in self.plan.signal_order}
