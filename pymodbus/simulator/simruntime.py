"""Simulator runtime implementation.

**REMARK** This class is internal to the server/simulator,.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

from ..constants import ExcCodes
from .simdevice import SimDevice
from .simutils import DataType, SimUtils


class SimReadResult(list):
    """Snapshot returned by a read transaction."""

    def __init__(
        self,
        values: list[int] | list[bool],
        runtime: SimRuntime,
        block_id: str,
        clear_ranges: tuple[tuple[int, int, bool], ...],
        lock: asyncio.Lock,
    ):
        """Create read result and retain the pending transaction."""
        super().__init__(values)
        self._runtime = runtime
        self._block_id = block_id
        self._clear_ranges = clear_ranges
        self._lock = lock
        self._closed = False

    async def commit(self) -> None:
        """Clear values after the response was constructed successfully."""
        if self._closed:
            return
        self._closed = True
        try:
            self._runtime.clear_read_ranges(self._block_id, self._clear_ranges)
        finally:
            self._lock.release()

    async def rollback(self) -> None:
        """Release the snapshot without clearing values."""
        if self._closed:
            return
        self._closed = True
        self._lock.release()


class SimRuntime:
    """Memory setup for device."""

    _fx_mapper = {2: "d", 4: "i"}
    _fx_mapper.update([(i, "h") for i in (3, 6, 16, 22, 23)])
    _fx_mapper.update([(i, "c") for i in (1, 5, 15)])

    def __init__(self, device: SimDevice):
        """Build device memory."""
        self.action = device.action
        self.use_bit_addressing = device.use_bit_addressing
        build = device.build_device()
        self.read_groups = device.build_read_groups()
        self.lock = asyncio.Lock()
        self.snapshot_barrier: Callable[[], Awaitable[None]] | None = None
        self.block: dict[str, tuple[int, int, list[int], list[int], int]] = {}
        if not isinstance(build, dict):
            block_start = build[0]
            self.block["x"] = (block_start, len(build[2]), build[1], build[2], 0)
            self.read_groups["x"] = [
                (start - block_start, length)
                for start, length in self.read_groups["x"]
            ]
            return
        for block_id in ("c", "d", "h", "i"):
            entry = build[block_id]
            self.block[block_id] = (entry[0], len(entry[1]), entry[1], entry[2], 0)
            if block_id in {"h", "i"}:
                block_start = entry[0]
                self.read_groups[block_id] = [
                    (start - block_start, length)
                    for start, length in self.read_groups[block_id]
                ]

    @staticmethod
    def __intersects(
        start: int, length: int, other_start: int, other_length: int
    ) -> bool:
        """Check whether two ranges intersect."""
        return start < other_start + other_length and other_start < start + length

    def __check_block(
        self,
        block_id: str,
        offset: int,
        count: int,
        values: list[int] | list[bool] | None = None,
    ) -> ExcCodes | None:
        """Check block request."""
        _start, _length, _registers, flags, _version = self.block[block_id]
        for i in range(count):
            address = offset + i
            if flags[address] & SimUtils.RunTimeFlag_TYPE == DataType.INVALID:
                return ExcCodes.ILLEGAL_ADDRESS
            if values is not None and flags[address] & SimUtils.RunTimeFlag_READONLY:
                return ExcCodes.ILLEGAL_ADDRESS
        return None

    async def __run_action(
        self,
        func_code: int,
        block_id: str,
        address: int,
        count: int,
        values: list[int] | list[bool] | None,
    ) -> ExcCodes | None:
        """Run the optional device action."""
        if not self.action:
            return None
        start, _length, registers, _flags, _version = self.block[block_id]
        return await self.action(
            func_code, start, address, count, registers, values
        )

    def __bump_version(self, block_id: str) -> None:
        """Record an atomic state change."""
        start, length, registers, flags, version = self.block[block_id]
        self.block[block_id] = (start, length, registers, flags, version + 1)

    def __register_clear_ranges(
        self, block_id: str, offset: int, count: int
    ) -> tuple[tuple[int, int, bool], ...]:
        """Find configured register groups touched by this request."""
        return tuple(
            (group_start, group_length, False)
            for group_start, group_length in self.read_groups.get(block_id, [])
            if self.__intersects(offset, count, group_start, group_length)
        )

    def clear_read_ranges(
        self, block_id: str, clear_ranges: tuple[tuple[int, int, bool], ...]
    ) -> None:
        """Clear configured groups included in a successful read."""
        _start, _length, registers, _flags, _version = self.block[block_id]
        for start, length, bit_range in clear_ranges:
            if bit_range:
                bits = SimUtils.registersToBits(registers)
                bits[start : start + length] = [False] * length
                registers[0:] = SimUtils.bitsToRegisters(bits)
            else:
                registers[start : start + length] = [0] * length
        self.__bump_version(block_id)

    async def __snapshot_registers(
        self, block_id: str, offset: int, count: int
    ) -> list[int]:
        """Copy one consistent register version."""
        while True:
            await self.lock.acquire()
            _start, _length, registers, _flags, version = self.block[block_id]
            snapshot = [registers[offset]]
            if self.snapshot_barrier:
                self.lock.release()
                await self.snapshot_barrier()
                await self.lock.acquire()
            _start, _length, registers, _flags, current_version = self.block[block_id]
            if current_version == version:
                snapshot.extend(registers[offset + 1 : offset + count])
                return snapshot
            self.lock.release()

    async def __read_result(
        self,
        block_id: str,
        values: list[int] | list[bool],
        clear_ranges: tuple[tuple[int, int, bool], ...],
        transaction: bool,
    ) -> list[int] | list[bool] | SimReadResult:
        """Return a snapshot and defer or immediately perform the clear."""
        if transaction:
            return SimReadResult(values, self, block_id, clear_ranges, self.lock)
        try:
            self.clear_read_ranges(block_id, clear_ranges)
        finally:
            self.lock.release()
        return values

    async def __write_registers(
        self,
        func_code: int,
        block_id: str,
        address: int,
        offset: int,
        count: int,
        values: list[int],
    ) -> list[int] | ExcCodes:
        """Write registers atomically."""
        if result := await self.__run_action(
            func_code, block_id, address, count, values
        ):
            return result
        async with self.lock:
            if result := self.__check_block(block_id, offset, count, values):
                return result
            _start, _length, registers, _flags, _version = self.block[block_id]
            registers[offset : offset + count] = values
            self.__bump_version(block_id)
            return list(values)

    async def __write_bits(
        self,
        func_code: int,
        block_id: str,
        address: int,
        offset: int,
        bit_offset: int,
        count: int,
        reg_count: int,
        values: list[bool],
    ) -> list[bool] | ExcCodes:
        """Write bits atomically."""
        if result := await self.__run_action(
            func_code, block_id, address, count, values
        ):
            return result
        async with self.lock:
            if result := self.__check_block(block_id, offset, reg_count, values):
                return result
            _start, _length, registers, _flags, _version = self.block[block_id]
            bits = SimUtils.registersToBits(registers[offset : offset + reg_count])
            bits[bit_offset : bit_offset + count] = values
            registers[offset : offset + reg_count] = SimUtils.bitsToRegisters(bits)
            self.__bump_version(block_id)
            return list(values)

    async def get_bit_block(
        self,
        block_id: str,
        func_code: int,
        address: int,
        count: int,
        values: list[bool] | None,
        transaction: bool = False,
    ) -> list[bool] | ExcCodes:
        """Handle coils and discrete input."""
        start_address, register_count, _registers, _flags, _version = self.block[
            block_id
        ]
        offset = (
            int(address / 16) if self.use_bit_addressing else address
        ) - start_address
        bit_offset = address % 16
        reg_count = (bit_offset + count + 15) // 16
        if offset < 0 or offset + reg_count > register_count:
            return ExcCodes.ILLEGAL_ADDRESS
        if result := self.__check_block(block_id, offset, reg_count, values):
            return result
        if values is not None:
            return await self.__write_bits(
                func_code,
                block_id,
                address,
                offset,
                bit_offset,
                count,
                reg_count,
                values,
            )
        if result := await self.__run_action(func_code, block_id, address, count, None):
            return result
        await self.lock.acquire()
        if result := self.__check_block(block_id, offset, reg_count):
            self.lock.release()
            return result
        _start, _length, registers, _flags, _version = self.block[block_id]
        register_snapshot = list(registers[offset : offset + reg_count])
        result_bits = SimUtils.registersToBits(register_snapshot)[
            bit_offset : bit_offset + count
        ]
        if self.use_bit_addressing:
            bit_start = offset * 16 + bit_offset
            clear_ranges = tuple(
                (group_start, group_length, True)
                for group_start, group_length in self.read_groups.get(block_id, [])
                if self.__intersects(bit_start, count, group_start, group_length)
            )
        else:
            clear_ranges = self.__register_clear_ranges(block_id, offset, reg_count)
        return await self.__read_result(
            block_id, result_bits, clear_ranges, transaction
        )

    async def get_reg_block(
        self,
        block_id: str,
        func_code: int,
        address: int,
        count: int,
        values: list[int] | None,
        transaction: bool = False,
    ) -> list[int] | ExcCodes:
        """Handle holding registers and input registers."""
        start_address, register_count, _registers, _flags, _version = self.block[
            block_id
        ]
        offset = address - start_address
        if offset < 0 or offset + count > register_count:
            return ExcCodes.ILLEGAL_ADDRESS
        if result := self.__check_block(block_id, offset, count, values):
            return result
        if values is not None:
            return await self.__write_registers(
                func_code, block_id, address, offset, count, values
            )
        if result := await self.__run_action(func_code, block_id, address, count, None):
            return result
        snapshot = await self.__snapshot_registers(block_id, offset, count)
        clear_ranges = self.__register_clear_ranges(block_id, offset, count)
        return await self.__read_result(block_id, snapshot, clear_ranges, transaction)

    async def get_block(
        self,
        func_code: int,
        address: int,
        count: int,
        values: list[int] | list[bool] | None,
        transaction: bool = False,
    ) -> list[int] | list[bool] | ExcCodes:
        """Calculate offset."""
        if (fc_block := self._fx_mapper.get(func_code, "z")) == "z":
            raise AttributeError("Datastore, not supported function code")
        block_id = "x" if "x" in self.block else fc_block
        if fc_block in {"c", "d"}:
            return await self.get_bit_block(
                block_id,
                func_code,
                address,
                count,
                cast(list[bool], values),
                transaction,
            )
        return await self.get_reg_block(
            block_id,
            func_code,
            address,
            count,
            cast(list[int], values),
            transaction,
        )

    async def async_getValues(
        self,
        func_code: int,
        address: int,
        count: int,
        transaction: bool = False,
    ) -> list[int] | list[bool] | ExcCodes:
        """Get `count` values from datastore."""
        return await self.get_block(
            func_code, address, count, None, transaction=transaction
        )

    async def async_setValues(
        self, func_code: int, address: int, values: list[int] | list[bool]
    ) -> ExcCodes | None:
        """Set the datastore with the supplied values."""
        block = await self.get_block(func_code, address, len(values), values)
        return block if isinstance(block, ExcCodes) else None
