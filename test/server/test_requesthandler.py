"""Test server asyncio."""

from unittest import mock

import pytest

from pymodbus.constants import ModbusStatus
from pymodbus.exceptions import ModbusIOException, NoSuchIdException
from pymodbus.framer import FramerRTU, FramerType
from pymodbus.pdu import ExceptionResponse
from pymodbus.pdu.bit_message import ReadCoilsRequest
from pymodbus.pdu.diag_message import (
    ClearCountersRequest,
    ForceListenOnlyModeRequest,
    RestartCommunicationsOptionRequest,
    ReturnBusMessageCountRequest,
    ReturnDeviceMessageCountRequest,
    ReturnQueryDataRequest,
)
from pymodbus.pdu.diagnostics import DiagnosticCounters
from pymodbus.pdu.pdu import ModbusPDU
from pymodbus.server import ModbusBaseServer
from pymodbus.simulator import DataType, SimData, SimDevice
from pymodbus.transport import CommParams, CommType


def rtu_frame(pdu: ModbusPDU) -> bytes:
    """Build an RTU frame for a populated PDU."""
    from pymodbus.pdu import DecodePDU

    return FramerRTU(DecodePDU(True)).buildFrame(pdu)


class TestRequesthandler:
    """Test for the pymodbus.server.startstop module."""

    @pytest.fixture
    async def requesthandler(self):
        """Fixture to provide base_server."""
        server = ModbusBaseServer(
            CommParams(
                comm_type=CommType.TCP,
                comm_name="server_listener",
                reconnect_delay=0.0,
                reconnect_delay_max=0.0,
                timeout_connect=0.0,
                source_address=("0", 0),
            ),
            SimDevice(0, SimData(0, datatype=DataType.REGISTERS, values=[17] * 100)),
            False,
            False,
            None,
            FramerType.SOCKET,
            None,
            None,
            None,
            [],
        )
        conn = server.callback_new_connection()
        conn.pdu_send = mock.Mock(return_value=True)  # type: ignore[method-assign]
        return conn

    @pytest.fixture
    async def diagnostic_handler(self):
        """Provide an RTU handler backed by two logical devices."""
        server = ModbusBaseServer(
            CommParams(
                comm_type=CommType.TCP,
                comm_name="server_listener",
                reconnect_delay=0.0,
                reconnect_delay_max=0.0,
                timeout_connect=0.0,
                source_address=("0", 0),
            ),
            [
                SimDevice(
                    1,
                    simdata=(
                        [SimData(0, datatype=DataType.BITS, values=[True])],
                        [SimData(0, datatype=DataType.BITS, values=[False])],
                        [SimData(0)],
                        [SimData(0)],
                    ),
                ),
                SimDevice(
                    2,
                    simdata=(
                        [SimData(0, datatype=DataType.BITS, values=[True])],
                        [SimData(0, datatype=DataType.BITS, values=[False])],
                        [SimData(0)],
                        [SimData(0)],
                    ),
                ),
            ],
            False,
            True,
            None,
            FramerType.RTU,
            None,
            None,
            None,
            [],
        )
        conn = server.callback_new_connection()
        conn.pdu_send = mock.Mock(return_value=True)  # type: ignore[method-assign]
        conn.handle_later = mock.Mock()
        return conn

    async def test_requesthandler(self, requesthandler):
        """Test __init__."""

    async def test_rh_callback_data(self, requesthandler):
        """Test __init__."""
        with mock.patch(
            "pymodbus.transaction.TransactionManager.callback_data"
        ) as cb_data:
            cb_data.side_effect = ModbusIOException
            data = b"012"
            assert len(data) == requesthandler.callback_data(data, None)

    async def test_rh_callback_data_undecodable_echoes_identity(self, requesthandler):
        """Undecodable FC exception must echo framing tid/dev_id (#2990)."""
        peer = ("192.0.2.1", 5020)
        with mock.patch(
            "pymodbus.transaction.TransactionManager.callback_data"
        ) as cb_data:
            exc = ModbusIOException(
                "Unable to decode request",
                transaction_id=0x000A,
                dev_id=7,
            )
            cb_data.side_effect = exc
            data = b"\x00\x0a\x00\x00\x00\x06\x07\x0a\x00\x00\x00\x01"
            assert len(data) == requesthandler.callback_data(data, peer)

        requesthandler.pdu_send.assert_called_once()
        response = requesthandler.pdu_send.call_args.args[0]
        assert isinstance(response, ExceptionResponse)
        assert response.transaction_id == 0x000A
        assert response.dev_id == 7
        assert response.function_code == 0x80
        assert response.exception_code == 0x01
        assert requesthandler.pdu_send.call_args.kwargs.get("addr") == peer

    async def test_rh_callback_data_undecodable_without_framing_attrs(
        self, requesthandler
    ):
        """Missing framing attrs fall back to zeros rather than crashing."""
        with mock.patch(
            "pymodbus.transaction.TransactionManager.callback_data"
        ) as cb_data:
            cb_data.side_effect = ModbusIOException("Unable to decode request")
            data = b"garbage"
            assert len(data) == requesthandler.callback_data(data, None)

        response = requesthandler.pdu_send.call_args.args[0]
        assert response.transaction_id == 0
        assert response.dev_id == 0
        assert response.function_code == 0x80

    async def test_rh_callback_data_undecodable_real_socket_frame(self, requesthandler):
        """End-to-end: undecodable socket FC echoes MBAP transaction id."""
        # MBAP: tid=0x1234, pid=0, len=6, uid=1, PDU: FC=0x09 + 4 data bytes
        frame = b"\x12\x34\x00\x00\x00\x06\x01\x09\x00\x00\x00\x01"
        assert len(frame) == requesthandler.callback_data(frame, None)

        requesthandler.pdu_send.assert_called_once()
        response = requesthandler.pdu_send.call_args.args[0]
        assert isinstance(response, ExceptionResponse)
        assert response.transaction_id == 0x1234
        assert response.dev_id == 1
        assert response.function_code == 0x80
        assert response.exception_code == 0x01

    async def test_diagnostics_two_devices_and_listen_only(self, diagnostic_handler):
        """Bus, device, exception, broadcast, and silence counters stay separate."""
        server = diagnostic_handler.server
        diagnostics = server.diagnostics

        async def feed(pdu: ModbusPDU, *, bad_crc: bool = False) -> None:
            frame = rtu_frame(pdu)
            if bad_crc:
                frame = frame[:-1] + bytes([frame[-1] ^ 0x01])
            diagnostic_handler.callback_data(frame, None)
            await diagnostic_handler.handle_request()

        await feed(ReadCoilsRequest(address=999, count=1, dev_id=1), bad_crc=True)
        await feed(ReadCoilsRequest(address=999, count=1, dev_id=1))
        await feed(ReadCoilsRequest(address=0, count=1, dev_id=2))
        await feed(ReturnQueryDataRequest(message=b"\x12\x34", dev_id=0))
        await feed(ClearCountersRequest(dev_id=1))

        assert diagnostics.bus.BusMessage == 4
        assert diagnostics.device(1).counters.DeviceMessage == 0
        assert diagnostics.device(1).counters.BusExceptionError == 0
        assert diagnostics.device(1).counters.DeviceNoResponse == 0
        assert not diagnostics.device(1).listen_only
        assert diagnostics.device(2).counters.DeviceMessage == 2
        assert diagnostics.device(2).counters.DeviceNoResponse == 1

        await feed(ForceListenOnlyModeRequest(dev_id=1))
        await feed(ReadCoilsRequest(address=0, count=1, dev_id=2))
        await feed(ReadCoilsRequest(address=0, count=1, dev_id=1))

        assert diagnostics.bus.BusMessage == 7
        assert diagnostics.bus.BusCommunicationError == 1
        assert diagnostics.bus.BusExceptionError == 1
        assert diagnostics.device(1).counters.DeviceMessage == 2
        assert diagnostics.device(1).counters.DeviceNoResponse == 2
        assert diagnostics.device(2).counters.DeviceMessage == 3
        assert diagnostics.device(2).counters.DeviceNoResponse == 1
        assert diagnostics.device(1).listen_only
        assert diagnostic_handler.pdu_send.call_count == 4

        bus_count_response = await diagnostic_handler._datastore_update(
            ReturnBusMessageCountRequest(dev_id=1), 1, diagnostics
        )
        assert bus_count_response.message == 7
        device_count_response = await diagnostic_handler._datastore_update(
            ReturnDeviceMessageCountRequest(dev_id=1), 1, diagnostics
        )
        assert device_count_response.message == 2

        await feed(ReadCoilsRequest(address=0, count=1, dev_id=1))
        assert diagnostic_handler.pdu_send.call_count == 4
        await feed(
            RestartCommunicationsOptionRequest(
                message=ModbusStatus.OFF, dev_id=1
            )
        )
        assert diagnostic_handler.pdu_send.call_count == 4
        assert diagnostics.device(1).listen_only
        await feed(
            RestartCommunicationsOptionRequest(
                message=ModbusStatus.ON, dev_id=1
            )
        )
        assert diagnostic_handler.pdu_send.call_count == 4
        await feed(ReadCoilsRequest(address=0, count=1, dev_id=1))

        assert not diagnostics.device(1).listen_only
        assert diagnostics.bus.BusMessage == 11
        assert diagnostics.device(1).counters.DeviceMessage == 1
        assert diagnostics.device(1).counters.DeviceNoResponse == 0
        assert diagnostic_handler.pdu_send.call_count == 5

    def test_diagnostic_counters_wrap_at_16_bits(self):
        """All diagnostic counters use the fixed Modbus 16-bit wrap rule."""
        counters = DiagnosticCounters()
        counters.values["BusMessage"] = 0xFFFF

        counters.increment("BusMessage")

        assert counters.BusMessage == 0

    async def test_rh_handle_request(self, requesthandler):
        """Test __init__."""
        requesthandler.last_pdu = None
        await requesthandler.handle_request()
        requesthandler.last_pdu = ExceptionResponse(17)
        await requesthandler.handle_request()
        requesthandler.last_pdu.datastore_update = mock.AsyncMock()  # type: ignore[method-assign]
        requesthandler.server.broadcast_enable = True
        requesthandler.last_pdu.dev_id = 0
        await requesthandler.handle_request()
        requesthandler.last_pdu.datastore_update.side_effect = NoSuchIdException
        await requesthandler.handle_request()
        requesthandler.server.ignore_missing_devices = True
        await requesthandler.handle_request()

    async def test_rh_server_send(self, requesthandler):
        """Test __init__."""
        requesthandler.server_send(None, None)
        requesthandler.server_send(ExceptionResponse(17), None)
