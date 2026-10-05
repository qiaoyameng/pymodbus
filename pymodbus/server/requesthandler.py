"""Implementation of a Threaded Modbus Server."""

from __future__ import annotations

import asyncio
import traceback

from ..constants import ExcCodes
from ..exceptions import ModbusIOException, NoSuchIdException
from ..logging import Log
from ..pdu import ExceptionResponse
from ..pdu.diag_message import (
    ForceListenOnlyModeRequest,
    RestartCommunicationsOptionRequest,
)
from ..transaction import TransactionManager
from ..transport import CommParams


class ServerRequestHandler(TransactionManager):
    """Handle client connection."""

    def __init__(self, owner, trace_packet, trace_pdu, trace_connect):
        """Initialize."""
        params = CommParams(
            comm_name="server",
            comm_type=owner.comm_params.comm_type,
            reconnect_delay=0.0,
            reconnect_delay_max=0.0,
            timeout_connect=0.0,
            host=owner.comm_params.source_address[0],
            port=owner.comm_params.source_address[1],
            handle_local_echo=owner.comm_params.handle_local_echo,
        )
        self.server = owner
        self.running = False
        framer = owner.framer(owner.decoder)
        super().__init__(
            params,
            framer,
            0,
            True,
            trace_packet,
            trace_pdu,
            trace_connect,
        )
        self.framer.checksum_error_callback = self._handle_checksum_error

    def _handle_checksum_error(self, _dev_id: int) -> None:
        """Count a frame rejected by the CRC/LRC check (bus level)."""
        self.server.diagnostics.bus_communication_error()

    def callback_disconnected(self, exc: Exception | None) -> None:
        """Call when connection is lost."""
        super().callback_disconnected(exc)
        if exc is None:
            Log.debug(
                "Handler for stream [{}] has been canceled", self.comm_params.comm_name
            )
        else:
            Log.debug(
                "Client Disconnection {} due to {}",
                self.comm_params.comm_name,
                exc,
            )
        self.running = False

    def callback_data(self, data: bytes, addr: tuple | None = None) -> int:
        """Handle received data."""
        try:
            used_len = super().callback_data(data, addr)
        except ModbusIOException as exc:
            # Undecodable but framed (checksum valid) messages are still
            # messages detected on the bus; an illegal function response is
            # returned so the master is informed.
            self.server.diagnostics.bus_message()
            function_code = 0x00 if exc.fcode is None else exc.fcode
            response = ExceptionResponse(
                function_code,
                exception_code=ExcCodes.ILLEGAL_FUNCTION,
                device_id=exc.dev_id,
                transaction=exc.transaction_id,
            )
            if self.server.broadcast_enable and not exc.dev_id:
                # Broadcast frames never produce a response.
                return len(data)
            if exc.dev_id:
                self.server.diagnostics.device_message(exc.dev_id)
                if not self.server.diagnostics.is_listen_only(exc.dev_id):
                    self.server.diagnostics.device_exception(exc.dev_id)
                    self.server_send(response, addr)
                    return len(data)
                self.server.diagnostics.device_no_response(exc.dev_id)
                return len(data)
            self.server_send(response, addr)
            return len(data)
        if self.last_pdu:
            # A complete frame with a valid checksum was detected on the bus.
            self.server.diagnostics.bus_message()
            self.loop.call_soon(self.handle_later)
        return used_len

    def handle_later(self):
        """Change sync (async not allowed in call_soon) to async."""
        asyncio.run_coroutine_threadsafe(self.handle_request(), self.loop)

    async def handle_request(self):
        """Handle request."""
        if not self.last_pdu:
            return
        request = self.last_pdu
        diagnostics = self.server.diagnostics
        is_broadcast = bool(self.server.broadcast_enable and not request.dev_id)

        if is_broadcast:
            # Broadcast frames are executed on every device but never answered.
            for dev_id in self.server.context.device_ids():
                diagnostics.device_message(dev_id)
                diagnostics.device_no_response(dev_id)
                await request.datastore_update(
                    self.server.context,
                    dev_id,
                    diagnostics,
                )
            return

        diagnostics.device_message(request.dev_id)

        # In listen-only mode the device does not respond and does not
        # process requests, except for the restart communications option
        # which is the documented way to leave listen-only mode.
        if diagnostics.is_listen_only(request.dev_id) and not isinstance(
            request, RestartCommunicationsOptionRequest
        ):
            diagnostics.device_no_response(request.dev_id)
            return

        try:
            response = await request.datastore_update(
                self.server.context,
                request.dev_id,
                diagnostics,
            )

        except NoSuchIdException:
            if self.server.ignore_missing_devices:
                Log.debug(
                    "ignoring request for unknown device id: {}", request.dev_id
                )
                diagnostics.device_no_response(request.dev_id)
                return  # the client will simply timeout waiting for a response
            Log.error("requested device id does not exist: {}", request.dev_id)
            response = ExceptionResponse(
                request.function_code, ExcCodes.GATEWAY_NO_RESPONSE
            )
        except Exception as exc:  # pylint: disable=broad-except
            Log.error(
                "Datastore unable to fulfill request: {}; {}",
                exc,
                traceback.format_exc(),
            )
            response = ExceptionResponse(
                request.function_code, ExcCodes.DEVICE_FAILURE
            )
        if response is None:
            diagnostics.device_no_response(request.dev_id)
            return
        if response.isError():
            diagnostics.device_exception(request.dev_id)
            if response.exception_code == ExcCodes.NEGATIVE_ACKNOWLEDGE:
                diagnostics.device(request.dev_id).counters.increment("DeviceNAK")
            elif response.exception_code == ExcCodes.DEVICE_BUSY:
                diagnostics.device(request.dev_id).counters.increment("DeviceBusy")
        # Force listen only defines no response; the device goes silent after
        # accepting the request.
        if isinstance(request, ForceListenOnlyModeRequest):
            diagnostics.device_no_response(request.dev_id)
            return
        response.transaction_id = request.transaction_id
        response.dev_id = request.dev_id
        self.server_send(response, self.last_addr)

    def server_send(self, pdu, addr):
        """Send message."""
        if not pdu:
            Log.debug("Skipping sending response!!")
        else:
            self.pdu_send(pdu, addr=addr)
