"""Server-held Modbus diagnostic state."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..constants import ExcCodes


@dataclass
class DiagnosticCounters:
    """Counters that wrap at 16 bits."""

    values: dict[str, int] = field(default_factory=dict)

    def __getattr__(self, name: str) -> int:
        if name in {"values"}:
            raise AttributeError(name)
        return self.values.get(name, 0)

    def increment(self, *names: str, amount: int = 1) -> None:
        """Increment one or more counters."""
        for name in names:
            self.values[name] = (self.values.get(name, 0) + amount) & 0xFFFF

    def reset(self) -> None:
        """Reset all counters."""
        self.values.clear()


@dataclass
class DeviceDiagnostics:
    """Diagnostic state for one logical device."""

    listen_only: bool = False
    counters: DiagnosticCounters = field(default_factory=DiagnosticCounters)
    register: list[bool] = field(default_factory=lambda: [False] * 16)

    def clear(self) -> None:
        """Clear device counters and leave normal communications enabled."""
        self.counters.reset()
        self.register = [False] * 16
        self.listen_only = False

    def clear_counters(self) -> None:
        """Clear counters without leaving listen-only mode."""
        self.counters.reset()
        self.register = [False] * 16

    def restart(self) -> bool:
        """Restart communications and return whether the device was listening."""
        was_listening = self.listen_only
        self.clear()
        return was_listening


class ServerDiagnostics:
    """Bus-wide and per-device diagnostic state for a server."""

    def __init__(self) -> None:
        """Initialize diagnostic state."""
        self.bus = DiagnosticCounters()
        self.devices: dict[int, DeviceDiagnostics] = {}

    def device(self, device_id: int) -> DeviceDiagnostics:
        """Return diagnostics for a logical device."""
        if device_id not in self.devices:
            self.devices[device_id] = DeviceDiagnostics()
        return self.devices[device_id]

    def valid_frame(self) -> None:
        """Record a frame that passed framing and PDU validation."""
        self.bus.increment("BusMessage")

    def device_frame(self, device_id: int) -> None:
        """Record a valid frame dispatched to one device."""
        self.device(device_id).counters.increment("DeviceMessage")

    def checksum_error(self) -> None:
        """Record a frame rejected by its checksum."""
        self.bus.increment("BusCommunicationError")

    def clear_overrun_count(self) -> None:
        """Clear the bus character-overrun counter."""
        self.bus.values.pop("BusCharacterOverrun", None)

    def response(self, device_id: int, exception_code: int | None) -> None:
        """Record an exception response or successful response delivery."""
        if exception_code is None:
            return
        counters = self.device(device_id).counters
        self.bus.increment("BusExceptionError")
        if exception_code == ExcCodes.NEGATIVE_ACKNOWLEDGE:
            counters.increment("DeviceNAK")
        elif exception_code == ExcCodes.DEVICE_BUSY:
            counters.increment("DEVICE_BUSY")
        if device_id:
            counters.increment("BusExceptionError")

    def no_response(self, device_id: int) -> None:
        """Record a request that was not answered."""
        self.device(device_id).counters.increment("DeviceNoResponse")

    def clear_device(self, device_id: int) -> None:
        """Clear counters for one addressed logical device."""
        self.device(device_id).clear_counters()

    def restart_device(self, device_id: int) -> bool:
        """Restart one logical device's communications state."""
        return self.device(device_id).restart()

    def set_listen_only(self, device_id: int) -> None:
        """Place one logical device in listen-only mode."""
        self.device(device_id).listen_only = True

    def is_listen_only(self, device_id: int) -> bool:
        """Return whether one logical device is in listen-only mode."""
        return self.device(device_id).listen_only
