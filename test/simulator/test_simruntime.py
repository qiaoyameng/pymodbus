"""Test SimCore."""

import asyncio

import pytest

from pymodbus.pdu.register_message import ReadHoldingRegistersRequest
from pymodbus.constants import ExcCodes
from pymodbus.simulator import DataType, SimData, SimDevice
from pymodbus.simulator.simruntime import SimRuntime
from pymodbus.simulator.simcore import SimCore


class TestSimRuntime:
    """Test simulator runtime component."""

    async def my_action(
        self,
        function_code,
        _start_address,
        _address,
        _count,
        current_registers,
        set_values,
    ):
        """Run action."""
        if function_code in {1, 3}:
            current_registers[1] = 17
        if function_code in {2, 4}:
            return ExcCodes.ILLEGAL_ADDRESS
        elif function_code == 15:
            set_values[0] = False
        elif function_code == 16:
            set_values[0] = 17

    sd_block = (
        [
            SimData(0, values=[14], datatype=DataType.BITS),
            SimData(16, values=[15], readonly=True, datatype=DataType.BITS),
            SimData(32, values=[16], datatype=DataType.BITS),
        ],
        [
            SimData(0, values=[14], datatype=DataType.BITS),
            SimData(16, values=[15], readonly=True, datatype=DataType.BITS),
            SimData(32, values=[16], datatype=DataType.BITS),
        ],
        [
            SimData(0, values=[14], datatype=DataType.REGISTERS),
            SimData(1, values=[15], readonly=True, datatype=DataType.REGISTERS),
            SimData(2, values=[16], datatype=DataType.REGISTERS),
        ],
        [
            SimData(0, values=[14], datatype=DataType.REGISTERS),
            SimData(1, values=[15], readonly=True, datatype=DataType.REGISTERS),
            SimData(2, values=[16], datatype=DataType.REGISTERS),
        ],
    )
    sd_shared = [
        SimData(0, datatype=DataType.REGISTERS, values=[14]),
        SimData(1, readonly=True, datatype=DataType.REGISTERS, values=[15]),
        SimData(2, datatype=DataType.REGISTERS, values=[16]),
    ]
    block_ids = {1: "c", 2: "d", 3: "h", 4: "i", 15: "c", 16: "h"}

    def test_simruntime_instanciate(self):
        """Test that simdata can be objects."""
        SimRuntime(SimDevice(0, self.sd_block))
        SimRuntime(SimDevice(0, self.sd_shared))

    @pytest.mark.parametrize("block", [False, True])
    @pytest.mark.parametrize("fc", range(1, 25))
    async def test_simruntime_fc(self, fc, block):
        """Test that simdata can be objects."""
        sd = SimDevice(1, simdata=(self.sd_block if block else self.sd_shared))
        rt = SimRuntime(sd)
        if fc in (1, 2, 3, 4, 5, 6, 15, 16, 22, 23):
            ret = await rt.get_block(fc, 1, 1, None)
            assert not isinstance(ret, ExcCodes)
        else:
            with pytest.raises(AttributeError):
                await rt.get_block(fc, 1, 1, None)

    @pytest.mark.parametrize("block", [True, False])
    @pytest.mark.parametrize(
        ("fc", "values", "expect"),
        [
            (1, None, [14, 17, 16, 0]),
            (3, None, [14, 17, 16, 0]),
            (4, None, -1),
            (2, None, -1),
            (15, [True, False], [12, 15, 16, 0]),
            (16, [12], [17, 15, 16, 0]),
        ],
    )
    async def test_simruntime_action(self, block, fc, values, expect):
        """Test that simdata can be objects."""
        rt = SimRuntime(
            SimDevice(
                1,
                action=self.my_action,
                simdata=(self.sd_block if block else self.sd_shared),
            )
        )
        block_id = self.block_ids[fc] if block else "x"
        count = len(values) if values else 2
        ret = await rt.get_block(fc, 0, count, values)
        if expect == -1:
            assert ret == ExcCodes.ILLEGAL_ADDRESS
        else:
            assert rt.block[block_id][2] == expect

    @pytest.mark.parametrize("block", [True, False])
    @pytest.mark.parametrize(
        ("fc", "addr", "count", "values", "expect"),
        [
            (1, 0, 2, None, [False, True]),
            (1, 0, 17, None, [False] + [True] * 3 + [False] * 12 + [True]),
            (2, 0, 2, None, [False, True]),
            (3, 0, 2, None, [14, 15]),
            (4, 0, 2, None, [14, 15]),
            (15, 0, 2, [True, False], [True, False]),
            (16, 0, 1, [12], [12]),
            (99, 0, 2, None, AttributeError),
            (3, 15, 2, None, ExcCodes.ILLEGAL_ADDRESS),
            (3, 2, 2, None, ExcCodes.ILLEGAL_ADDRESS),
            (16, 2, 2, [12, 12], ExcCodes.ILLEGAL_ADDRESS),
            (16, 1, 1, [12], ExcCodes.ILLEGAL_ADDRESS),
            (1, 64, 2, None, ExcCodes.ILLEGAL_ADDRESS),
            (15, 48, 17, [True] * 17, ExcCodes.ILLEGAL_ADDRESS),
        ],
    )
    async def test_simruntime_block(self, block, fc, addr, count, values, expect):
        """Test that simdata can be objects."""
        rt = SimRuntime(
            SimDevice(1, simdata=(self.sd_block if block else self.sd_shared))
        )
        if not isinstance(expect, list) and expect in {AttributeError}:
            with pytest.raises(expect):
                await rt.get_block(fc, addr, count, values)
        else:
            ret = await rt.get_block(fc, addr, count, values)
            assert ret == expect

    async def test_simruntime_getValues(self):
        """Test that simdata can be objects."""
        sd = SimDevice(0, simdata=SimData(10, values=15, datatype=DataType.REGISTERS))
        rt = SimRuntime(sd)
        result = await rt.async_getValues(0x03, 10, 1)
        assert result == [15]
        result = await rt.async_getValues(0x03, 8, 1)
        assert isinstance(result, ExcCodes)
        result = await rt.async_getValues(0x03, 9, 1)
        assert isinstance(result, ExcCodes)
        result = await rt.async_getValues(0x03, 11, 1)
        assert isinstance(result, ExcCodes)
        result = await rt.async_getValues(0x03, 12, 1)
        assert isinstance(result, ExcCodes)
        result = await rt.async_getValues(0x03, 15, 1)
        assert isinstance(result, ExcCodes)

    async def test_simruntime_setValues(self):
        """Test that simdata can be objects."""
        sd = SimDevice(0, simdata=SimData(10, values=15, datatype=DataType.REGISTERS))
        rt = SimRuntime(sd)
        result = await rt.async_setValues(0x03, 10, [1])
        assert not result
        result2 = await rt.async_getValues(0x03, 10, 1)
        assert result2 == [1]
        result3 = await rt.async_setValues(0x03, 15, [1])
        assert isinstance(result3, ExcCodes)

    async def test_simruntime_clear_on_read_range(self):
        """Only configured groups hit by a request are cleared after reading."""
        device = SimDevice(
            0,
            simdata=[
                SimData(0, values=10, datatype=DataType.REGISTERS),
                SimData(
                    1,
                    values=0x000B000C,
                    clear_on_read=True,
                    datatype=DataType.UINT32,
                ),
                SimData(3, values=13, datatype=DataType.REGISTERS),
            ],
        )
        runtime = SimRuntime(device)

        assert await runtime.async_getValues(0x03, 0, 1) == [10]
        assert runtime.block["x"][2] == [10, 11, 12, 13, 0]

        assert await runtime.async_getValues(0x03, 3, 1) == [13]
        assert runtime.block["x"][2] == [10, 11, 12, 13, 0]

        assert await runtime.async_getValues(0x03, 1, 1) == [11]
        assert runtime.block["x"][2] == [10, 0, 0, 13, 0]

        assert await runtime.async_getValues(0x03, 2, 2) == [0, 13]
        assert runtime.block["x"][2] == [10, 0, 0, 13, 0]

    async def test_simruntime_clear_invalid_request(self):
        """An illegal read must return an exception without clearing alarms."""
        device = SimDevice(
            0,
            simdata=[
                SimData(
                    0,
                    values=0x00010002,
                    clear_on_read=True,
                    datatype=DataType.UINT32,
                ),
            ],
        )
        runtime = SimRuntime(device)

        result = await runtime.async_getValues(0x03, 1, 2)

        assert result == ExcCodes.ILLEGAL_ADDRESS
        assert runtime.block["x"][2] == [1, 2, 0]

    async def test_simruntime_latch_is_per_request(self):
        """Each read starts with a fresh snapshot after the previous read commits."""
        device = SimDevice(
            0,
            simdata=[
                SimData(
                    0,
                    values=0x00010002,
                    latch_on_read=True,
                    datatype=DataType.UINT32,
                ),
                SimData(2, values=3, datatype=DataType.REGISTERS),
            ],
        )
        runtime = SimRuntime(device)

        first = await runtime.async_getValues(0x03, 0, 3)
        runtime.block["x"][2][0:3] = [4, 5, 6]
        second = await runtime.async_getValues(0x03, 0, 3)

        assert first == [1, 2, 3]
        assert second == [4, 5, 6]

    async def test_simruntime_latch_overlapping_and_partial_reads(self):
        """Overlapping requests and partial field reads use request snapshots."""
        device = SimDevice(
            0,
            simdata=[
                SimData(
                    0,
                    values=0x00010002,
                    latch_on_read=True,
                    datatype=DataType.UINT32,
                ),
                SimData(2, values=3, datatype=DataType.REGISTERS),
            ],
        )
        runtime = SimRuntime(device)

        first = await runtime.async_getValues(0x03, 0, 2, transaction=True)
        await first.rollback()
        runtime.block["x"][2][0:3] = [4, 5, 6]
        overlapping = await runtime.async_getValues(0x03, 1, 2)

        assert first == [1, 2]
        assert overlapping == [5, 6]

    async def test_simruntime_wide_snapshot_concurrent_update(self):
        """A wide counter is read either completely old or completely new."""
        start = asyncio.Event()
        barrier_reached = asyncio.Event()
        release_barrier = asyncio.Event()

        async def barrier():
            barrier_reached.set()
            await release_barrier.wait()

        device = SimDevice(
            0,
            simdata=[
                SimData(
                    0,
                    values=0x00010002,
                    latch_on_read=True,
                    datatype=DataType.UINT32,
                ),
            ],
        )
        runtime = SimRuntime(device)
        runtime.snapshot_barrier = barrier

        async def read_counter():
            return await runtime.async_getValues(0x03, 0, 2, transaction=True)

        async def update_counter():
            await start.wait()
            update_task = asyncio.create_task(
                runtime.async_setValues(0x10, 0, [0x0003, 0x0004])
            )
            await asyncio.sleep(0)
            await barrier_reached.wait()
            release_barrier.set()
            return await update_task

        start.set()
        snapshot, _update = await asyncio.gather(read_counter(), update_counter())
        assert snapshot in ([1, 2], [3, 4])
        assert runtime.block["x"][2][:2] == [3, 4]
        await snapshot.commit()

    async def test_simruntime_clear_after_response_construction(self):
        """Clear waits until the read response object was constructed."""
        core = SimCore(
            SimDevice(
                0,
                simdata=[
                    SimData(
                        0,
                        values=0x00010002,
                        clear_on_read=True,
                        datatype=DataType.UINT32,
                    )
                ],
            )
        )
        request = ReadHoldingRegistersRequest(address=0, count=2)

        response = await request.datastore_update(core, 0)

        assert response.registers == [1, 2]
        assert core.devices[0].block["x"][2] == [0, 0, 0]

    async def test_simruntime_latch_and_clear_combined(self):
        """The two read semantics can be used on the same field."""
        device = SimDevice(
            0,
            simdata=[
                SimData(
                    0,
                    values=0x00010002,
                    clear_on_read=True,
                    latch_on_read=True,
                    datatype=DataType.UINT32,
                )
            ],
        )
        runtime = SimRuntime(device)

        snapshot = await runtime.async_getValues(0x03, 0, 2, transaction=True)
        runtime.block["x"][2][0:2] = [3, 4]

        assert snapshot == [1, 2]
        await snapshot.commit()
        assert runtime.block["x"][2][:3] == [0, 0, 0]

    async def test_simruntime_clear_on_read_bits(self):
        """Bit clear ranges use bit coordinates and require an actual intersection."""
        empty_inputs = [SimData(0, count=32, values=False, datatype=DataType.BITS)]
        registers = [SimData(0, values=10, datatype=DataType.REGISTERS)]
        coils = [
            SimData(0, values=False, datatype=DataType.BITS),
            SimData(
                1,
                count=1,
                values=[
                    True,
                    False,
                    True,
                    False,
                    False,
                    False,
                ],
                clear_on_read=True,
                datatype=DataType.BITS,
            ),
            SimData(7, values=[False], datatype=DataType.BITS),
            SimData(8, values=[False] * 8, datatype=DataType.BITS),
        ]
        runtime = SimRuntime(
            SimDevice(0, simdata=(coils, empty_inputs, registers, registers))
        )

        assert await runtime.async_getValues(1, 7, 2) == [False, False]
        assert runtime.block["c"][2] == [0x000A, 0]

        assert await runtime.async_getValues(1, 0, 1) == [False]
        assert runtime.block["c"][2] == [0x000A, 0]

        assert await runtime.async_getValues(1, 3, 1) == [True]
        assert runtime.block["c"][2] == [0, 0]
