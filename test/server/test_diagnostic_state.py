"""End to end diagnostic state checks fed with short RTU frame sequences.

No real serial port is used: raw frames are pushed into the request handler
the same way the serial transport would do it.
"""

import asyncio
from unittest import mock

import pytest

from pymodbus.framer import FramerRTU, FramerType
from pymodbus.pdu import ReadCoilsRequest, ReadHoldingRegistersRequest
from pymodbus.pdu.diag_message import (
    ClearCountersRequest,
    DiagnosticBase,
    ForceListenOnlyModeRequest,
    RestartCommunicationsOptionRequest,
    ReturnBusCommunicationErrorCountRequest,
    ReturnBusMessageCountRequest,
    ReturnDeviceMessageCountRequest,
    ReturnDeviceNoResponseCountRequest,
)
from pymodbus.server import ModbusBaseServer
from pymodbus.simulator import DataType, SimData, SimDevice
from pymodbus.transport import CommParams, CommType


def rtu_frame(pdu) -> bytes:
    """Build a raw RTU frame from a populated request."""
    return FramerRTU(None).buildFrame(pdu)



def read_frame(dev_id, address=0, count=1):
    """Build a raw RTU read-holding-registers request frame."""
    return rtu_frame(
        ReadHoldingRegistersRequest(address=address, count=count, dev_id=dev_id)
    )


def corrupt(frame: bytes) -> bytes:
    """Flip a payload bit while keeping the (now wrong) CRC in place."""
    return frame[:1] + bytes([frame[1] ^ 0x01]) + frame[2:]


def diag(dev_id, sub_function, data=0):
    """Build a diagnostic (0x08) request."""
    request = DiagnosticBase(message=data, dev_id=dev_id)
    request.sub_function_code = sub_function
    return request


async def feed(handler, frame):
    """Feed one frame and let the scheduled request coroutine finish."""
    handler.callback_data(frame, None)
    # callback_data schedules handle_request via call_soon + run_coroutine_threadsafe
    for _ in range(5):
        await asyncio.sleep(0)


async def read_diag(handler, request):
    """Execute a diagnostic read directly against the server state."""
    return await request.datastore_update(
        handler.server.context, request.dev_id, handler.server.diagnostics
    )


@pytest.fixture
async def handler():
    """Provide a request handler with two logical devices on one bus."""
    server = ModbusBaseServer(
        CommParams(
            comm_type=CommType.SERIAL,
            comm_name="server_listener",
            reconnect_delay=0.0,
            reconnect_delay_max=0.0,
            timeout_connect=0.0,
            source_address=("loopback", 0),
        ),
        [
            SimDevice(1, SimData(0, count=100, values=17, datatype=DataType.REGISTERS)),
            SimDevice(2, SimData(0, count=100, values=17, datatype=DataType.REGISTERS)),
        ],
        False,
        True,  # broadcast enabled
        None,
        FramerType.RTU,
        None,
        None,
        None,
        [],
    )
    connection = server.callback_new_connection()
    connection.pdu_send = mock.Mock(return_value=True)  # type: ignore[method-assign]
    yield connection


class TestDiagnosticFrameSequence:
    """Drive counters and listen-only mode with real frame events."""

    async def test_valid_error_broadcast_and_no_response_counter_stages(self, handler):
        """Each counter moves at exactly the protocol stage it documents."""
        diagnostics = handler.server.diagnostics

        # 1) Valid frame addressed to device 1 -> normal response.
        await feed(handler, read_frame(1, count=2))
        assert diagnostics.counter_value("BusMessage", 1) == 1
        assert diagnostics.counter_value("BusCommunicationError", 1) == 0
        assert diagnostics.counter_value("DeviceMessage", 1) == 1
        assert diagnostics.counter_value("DeviceNoResponse", 1) == 0
        assert diagnostics.counter_value("DeviceMessage", 2) == 0
        assert handler.pdu_send.call_count == 1

        # 2) Valid frame but illegal address -> exception response.
        await feed(
            handler, read_frame(1, address=9000, count=2)
        )
        assert diagnostics.counter_value("BusMessage", 1) == 2
        assert diagnostics.counter_value("BusExceptionError", 1) == 1
        assert diagnostics.counter_value("DeviceMessage", 1) == 2
        assert diagnostics.counter_value("DeviceNoResponse", 1) == 0
        assert handler.pdu_send.call_count == 2

        # 3) CRC failure -> bus error counted, no device counters, no response.
        good = read_frame(2, count=2)
        await feed(handler, corrupt(good))
        assert diagnostics.counter_value("BusCommunicationError", 1) == 1
        assert diagnostics.counter_value("BusMessage", 1) == 2
        assert diagnostics.counter_value("DeviceMessage", 2) == 0
        assert handler.pdu_send.call_count == 2

        # 4) Broadcast frame -> every device processes it but nobody answers.
        await feed(
            handler, read_frame(0)
        )
        assert diagnostics.counter_value("BusMessage", 1) == 3
        assert diagnostics.counter_value("DeviceMessage", 1) == 3
        assert diagnostics.counter_value("DeviceNoResponse", 1) == 1
        assert diagnostics.counter_value("DeviceMessage", 2) == 1
        assert diagnostics.counter_value("DeviceNoResponse", 2) == 1
        assert handler.pdu_send.call_count == 2

    async def test_counters_are_readable_through_diagnostic_requests(self, handler):
        """Diagnostic read sub-functions return bus vs device values."""
        await feed(handler, read_frame(1))
        await feed(handler, read_frame(2))
        await feed(handler, read_frame(2))

        bus = await read_diag(handler, ReturnBusMessageCountRequest(dev_id=1))
        dev1 = await read_diag(handler, ReturnDeviceMessageCountRequest(dev_id=1))
        dev2 = await read_diag(handler, ReturnDeviceMessageCountRequest(dev_id=2))
        bus_err = await read_diag(
            handler, ReturnBusCommunicationErrorCountRequest(dev_id=1)
        )
        assert bus.message == 3  # shared bus counter, same from either device
        assert dev1.message == 1
        assert dev2.message == 2
        assert bus_err.message == 0

    async def test_clear_one_device_keeps_the_other_and_bus_intact(self, handler):
        """Clear counters (0x0A) only touches the addressed device."""
        diagnostics = handler.server.diagnostics
        await feed(handler, read_frame(1))
        await feed(
            handler, read_frame(1, address=9000)
        )
        await feed(handler, read_frame(2))

        await feed(handler, rtu_frame(ClearCountersRequest(dev_id=1)))

        assert diagnostics.counter_value("DeviceMessage", 1) == 0
        assert diagnostics.counter_value("BusExceptionError", 1) == 0
        assert diagnostics.counter_value("DeviceMessage", 2) == 1
        # bus counters are shared and survive a device clear
        assert diagnostics.counter_value("BusMessage", 1) == 4
        assert handler.pdu_send.call_count == 4  # clear counters itself is answered

    async def test_force_listen_only_blocks_dispatch_until_restart(self, handler):
        """Listen only silences the device; restart is the only recovery."""
        diagnostics = handler.server.diagnostics

        # Device 1 enters listen-only: request accepted but no response sent.
        await feed(handler, rtu_frame(ForceListenOnlyModeRequest(dev_id=1)))
        assert diagnostics.is_listen_only(1)
        assert diagnostics.counter_value("DeviceNoResponse", 1) == 1
        sent_after_listen = handler.pdu_send.call_count

        # Ordinary frames are counted but not dispatched/answered on device 1.
        await feed(handler, read_frame(1))
        assert diagnostics.counter_value("DeviceMessage", 1) == 2
        assert diagnostics.counter_value("DeviceNoResponse", 1) == 2
        assert handler.pdu_send.call_count == sent_after_listen

        # Device 2 on the same bus is unaffected and still answers.
        await feed(handler, read_frame(2))
        assert not diagnostics.is_listen_only(2)
        assert handler.pdu_send.call_count == sent_after_listen + 1

        # The restart communications option is processed even while silent.
        await feed(
            handler,
            rtu_frame(RestartCommunicationsOptionRequest(message=0xFF00, dev_id=1)),
        )
        assert not diagnostics.is_listen_only(1)
        assert handler.pdu_send.call_count == sent_after_listen + 2

        # After recovery normal dispatch resumes.
        await feed(handler, read_frame(1))
        assert diagnostics.counter_value("DeviceNoResponse", 1) == 2
        assert handler.pdu_send.call_count == sent_after_listen + 3

    async def test_restart_with_clear_resets_device_counters(self, handler):
        """Restart with data 0x0000 clears the device counters as per spec."""
        diagnostics = handler.server.diagnostics
        await feed(handler, rtu_frame(ForceListenOnlyModeRequest(dev_id=1)))
        await feed(handler, read_frame(1))
        assert diagnostics.counter_value("DeviceMessage", 1) == 2

        await feed(
            handler,
            rtu_frame(RestartCommunicationsOptionRequest(message=0x0000, dev_id=1)),
        )
        assert not diagnostics.is_listen_only(1)
        assert diagnostics.counter_value("DeviceMessage", 1) == 0
        assert diagnostics.counter_value("DeviceNoResponse", 1) == 0

    async def test_no_response_count_request_reports_value(self, handler):
        """0x0F returns the accumulated no-response count."""
        await feed(handler, rtu_frame(ForceListenOnlyModeRequest(dev_id=2)))
        await feed(handler, read_frame(2))

        response = await read_diag(
            handler, ReturnDeviceNoResponseCountRequest(dev_id=2)
        )
        assert response.message == 2

    async def test_counter_wraps_at_16_bits(self, handler):
        """Device counters wrap from 0xFFFF to 0x0000."""
        counters = handler.server.diagnostics.device(1).counters
        counters.DeviceMessage = 0xFFFF
        counters.increment("DeviceMessage")
        assert counters.DeviceMessage == 0
        counters.DeviceMessage = 0xFFFF
        counters.increment("DeviceMessage", 5)
        assert counters.DeviceMessage == 4

    async def test_diagnostic_frame_for_unused_device_still_answered(self, handler):
        """Sanity: an echo diagnostic on device 1 returns a response."""
        await feed(handler, rtu_frame(diag(1, 0x0000, data=b"\x12\x34")))
        assert handler.pdu_send.call_count == 1
        assert diagnostics_frame_is_echo(handler)


def diagnostics_frame_is_echo(handler):
    """Check the last sent frame carries the echo diagnostic response."""
    sent = handler.pdu_send.call_args.args[0]
    return sent.function_code == 0x08 and sent.message == b"\x12\x34"
