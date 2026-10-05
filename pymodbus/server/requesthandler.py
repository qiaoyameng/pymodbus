"""Implementation of a Threaded Modbus Server."""

from __future__ import annotations

import asyncio
import traceback

from ..constants import ExcCodes, ModbusStatus
from ..exceptions import ModbusIOException, NoSuchIdException
from ..logging import Log
from ..pdu import ExceptionResponse
from ..pdu.diag_message import DiagnosticBase
from ..pdu.diag_message import (
    ForceListenOnlyModeResponse,
    RestartCommunicationsOptionRequest,
)
from ..pdu.diagnostics import ServerDiagnostics
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
        framer.frame_error_callback = self._frame_checksum_error
        super().__init__(
            params,
            framer,
            0,
            True,
            trace_packet,
            trace_pdu,
            trace_connect,
        )

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
            function_code = 0x00 if exc.fcode is None else exc.fcode
            response = ExceptionResponse(
                function_code,
                exception_code=ExcCodes.ILLEGAL_FUNCTION,
                device_id=exc.dev_id,
                transaction=exc.transaction_id,
            )
            diagnostics = self.server.diagnostics
            diagnostics.valid_frame()
            diagnostics.response(exc.dev_id, ExcCodes.ILLEGAL_FUNCTION)
            self.server_send(response, addr)
            return len(data)
        if self.last_pdu:
            self.loop.call_soon(self.handle_later)
        return used_len

    def _frame_checksum_error(self, _device_id: int) -> None:
        """Record a checksum failure without sending a response."""
        self.server.diagnostics.checksum_error()

    def handle_later(self):
        """Change sync (async not allowed in call_soon) to async."""
        asyncio.run_coroutine_threadsafe(self.handle_request(), self.loop)

    async def handle_request(self):
        """Handle request."""
        request = self.last_pdu
        if not request:
            return
        diagnostics = self.server.diagnostics
        is_broadcast = self.server.broadcast_enable and not request.dev_id
        diagnostics.valid_frame()
        if is_broadcast:
            await self._handle_broadcast(request, diagnostics)
            return

        response = await self._dispatch(request, request.dev_id, diagnostics)
        if response is not None:
            await self._send_response(request, response)

    async def _handle_broadcast(self, request, diagnostics: ServerDiagnostics):
        """Dispatch one request to all logical devices without answering."""
        for dev_id in self.server.context.device_ids():
            await self._dispatch(request, dev_id, diagnostics, silent=True)

    async def _dispatch(
        self,
        request,
        device_id: int,
        diagnostics: ServerDiagnostics,
        *,
        silent=False,
    ):
        """Dispatch one request while enforcing per-device listen-only mode."""
        if diagnostics.is_listen_only(device_id) and not self._is_restart_request(
            request
        ):
            self._record_result(
                diagnostics, device_id, None, silent=True, count_message=True
            )
            return None
        try:
            response = await self._datastore_update(request, device_id, diagnostics)
        except NoSuchIdException:
            if self.server.ignore_missing_devices:
                Log.debug(
                    "ignoring request for unknown device id: {}", device_id
                )
                return None
            Log.error("requested device id does not exist: {}", device_id)
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
        self._record_result(
            diagnostics,
            device_id,
            response,
            silent=silent,
            count_message=True,
            skip_device_message=getattr(response, "clears_request_counter", False),
        )
        return response

    async def _datastore_update(self, request, device_id: int, diagnostics):
        """Pass diagnostics only to PDU handlers that explicitly accept them."""
        if isinstance(request, DiagnosticBase):
            return await request.datastore_update(
                self.server.context, device_id, diagnostics
            )
        return await request.datastore_update(self.server.context, device_id)

    async def _send_response(self, request, response):
        """Send a unicast response unless the PDU explicitly requires silence."""
        if not response or getattr(response, "no_response", False):
            return
        response.transaction_id = request.transaction_id
        response.dev_id = request.dev_id
        self.server_send(response, self.last_addr)

    @staticmethod
    def _is_restart_request(request) -> bool:
        """Return whether a request may recover a device from listen-only."""
        return (
            isinstance(request, RestartCommunicationsOptionRequest)
            and request.message == ModbusStatus.ON
        )

    @staticmethod
    def _record_result(
        diagnostics: ServerDiagnostics,
        device_id: int,
        response,
        silent: bool = False,
        count_message: bool = False,
        skip_device_message: bool = False,
    ) -> None:
        """Update counters for a dispatched response."""
        if count_message and not skip_device_message:
            diagnostics.device_frame(device_id)
        if silent:
            diagnostics.no_response(device_id)
        if isinstance(response, ForceListenOnlyModeResponse) and not silent:
            diagnostics.no_response(device_id)
        if not response:
            return
        exception_code = (
            response.exception_code
            if isinstance(response, ExceptionResponse)
            else None
        )
        diagnostics.response(device_id, exception_code)

    def server_send(self, pdu, addr):
        """Send message."""
        if not pdu:
            Log.debug("Skipping sending response!!")
        else:
            self.pdu_send(pdu, addr=addr)
