"""In-memory file record storage for FC20/FC21."""

from __future__ import annotations

from ..constants import ExcCodes


class ModbusFileRecordStore:
    """In-memory file record storage, isolated per device/file/record.

    Values are stored as 16-bit registers, nothing is persisted
    to disk or external databases.
    """

    MIN_FILE_NUMBER = 0x0001
    MAX_FILE_NUMBER = 0xFFFF
    MAX_RECORD_NUMBER = 0x270F
    MAX_RECORD_LENGTH = 0x007D

    def __init__(self) -> None:
        """Initialize an empty store."""
        self._registers: dict[tuple[int, int, int], int] = {}

    def validate(
        self, file_number: int, record_number: int, record_length: int
    ) -> ExcCodes | None:
        """Validate length and boundaries of a single sub record."""
        if not 1 <= record_length <= self.MAX_RECORD_LENGTH:
            return ExcCodes.ILLEGAL_VALUE
        if not self.MIN_FILE_NUMBER <= file_number <= self.MAX_FILE_NUMBER:
            return ExcCodes.ILLEGAL_ADDRESS
        if record_number + record_length - 1 > self.MAX_RECORD_NUMBER:
            return ExcCodes.ILLEGAL_ADDRESS
        return None

    def read(
        self, device_id: int, file_number: int, record_number: int, count: int
    ) -> list[int]:
        """Read `count` registers, unwritten registers read as 0."""
        return [
            self._registers.get((device_id, file_number, record_number + offset), 0)
            for offset in range(count)
        ]

    def write(
        self,
        device_id: int,
        file_number: int,
        record_number: int,
        values: list[int],
    ) -> None:
        """Write registers starting at record_number."""
        for offset, value in enumerate(values):
            self._registers[(device_id, file_number, record_number + offset)] = (
                value & 0xFFFF
            )
