"""Encapsulated Interface (MEI) Transport Messages."""

from __future__ import annotations

import struct
from typing import Any, NoReturn, cast

from ..constants import DeviceInformation, ExcCodes, MoreData
from ..datastore import ModbusServerContext
from ..exceptions import DeviceIdentificationError
from .decoders import DecodePDU
from .device import DeviceInformationFactory, ModbusControlBlock
from .exceptionresponse import ExceptionResponse
from .pdu import ModbusPDU


_MCB = ModbusControlBlock()


class _OutOfSpaceException(Exception):
    """Internal out of space exception."""

    # This exception exists here as a simple, local way to manage response
    # length control for the only MODBUS command which requires it under
    # standard, non-error conditions.
    #
    # See Page 5/50 of MODBUS Application Protocol Specification V1.1b3.

    def __init__(self, oid: int) -> None:
        self.oid = oid
        super().__init__()
        self.oid = oid


class _EncapsulatedInterfaceTransport(ModbusPDU):
    """Base PDU for dispatching Encapsulated Interface Transport messages."""

    function_code = 0x2B

    @classmethod
    def decode_sub_function_code(cls, data: bytes) -> int:
        """Decode the MEI type."""
        return int(data[2])

    def decode(self, data: bytes) -> None:
        """Decode the MEI type."""
        self.sub_function_code = int(data[0])

    async def datastore_update(
        self, context: ModbusServerContext, device_id: int
    ) -> ModbusPDU:
        """Reject unsupported MEI types."""
        _ = context, device_id
        return ExceptionResponse(self.function_code, ExcCodes.ILLEGAL_FUNCTION)


class ReadDeviceInformationRequest(ModbusPDU):
    """ReadDeviceInformationRequest."""

    function_code = 0x2B
    sub_function_code = 0x0E
    rtu_frame_size = 7

    def __init__(
        self,
        read_code: int | None = None,
        object_id: int = 0,
        dev_id: int = 1,
        transaction_id: int = 0,
    ) -> None:
        """Initialize a new instance."""
        super().__init__(transaction_id=transaction_id, dev_id=dev_id)
        self.read_code = read_code or DeviceInformation.BASIC
        self.object_id = object_id

    def encode(self) -> bytes:
        """Encode the request packet."""
        packet = struct.pack(
            ">BBB", self.sub_function_code, self.read_code, self.object_id
        )
        return packet

    @classmethod
    def decode_sub_function_code(cls, data: bytes) -> int:
        """Decode sub function code (1 byte)."""
        return int(data[2])

    def decode(self, data: bytes) -> None:
        """Decode data part of the message."""
        self.sub_function_code, self.read_code, self.object_id = struct.unpack(
            ">BBB", data[:3]
        )

    async def datastore_update(
        self, context: ModbusServerContext, device_id: int
    ) -> ModbusPDU:
        """Update diagnostic request on the given device."""
        _ = context
        if not 0x00 <= self.object_id <= 0xFF:
            return ExceptionResponse(self.function_code, ExcCodes.ILLEGAL_VALUE)
        if not 0x00 <= self.read_code <= 0x04:
            return ExceptionResponse(self.function_code, ExcCodes.ILLEGAL_VALUE)

        information = DeviceInformationFactory.get(_MCB, self.read_code, self.object_id)
        if self.read_code == DeviceInformation.SPECIFIC and (
            0x07 <= self.object_id < 0x80 or not information.get(self.object_id)
        ):
            return ExceptionResponse(self.function_code, ExcCodes.ILLEGAL_ADDRESS)
        return ReadDeviceInformationResponse(
            read_code=self.read_code,
            information=information,
            dev_id=device_id,
            transaction_id=self.transaction_id,
        )


class ReadDeviceInformationResponse(ModbusPDU):
    """ReadDeviceInformationResponse."""

    function_code = 0x2B
    sub_function_code = 0x0E

    @classmethod
    def calculateRtuFrameSize(cls, data: bytes) -> int:
        """Calculate the size of the message."""
        size = 8  # skip the header information
        if (data_len := len(data)) < size:
            return 999
        count = int(data[7])

        while count > 0:
            if data_len < size + 2:
                return 998
            _, object_length = struct.unpack(">BB", data[size : size + 2])
            size += object_length + 2
            count -= 1
        return size + 2

    def __init__(
        self,
        read_code: int | None = None,
        information: dict[int, Any] | None = None,
        dev_id: int = 1,
        transaction_id: int = 0,
    ) -> None:
        """Initialize a new instance."""
        super().__init__(transaction_id=transaction_id, dev_id=dev_id)
        self.read_code = read_code or DeviceInformation.BASIC
        self.information: dict[int, Any] = information or {}
        self.number_of_objects = 0
        self.conformity = 0x83  # I support everything right now
        self.next_object_id = 0x00
        self.more_follows = MoreData.NOTHING
        self.space_left = 253 - 6

    def _encode_object(self, object_id: int, data: bytes | ModbusPDU) -> bytes:
        """Encode object."""
        if not isinstance(data, bytes):
            data = data.encode()
        data_len = len(data)
        self.space_left -= 2 + data_len
        if self.space_left <= 0:
            raise _OutOfSpaceException(object_id)
        encoded_obj = struct.pack(">BB", object_id, data_len)
        encoded_obj += data
        self.number_of_objects += 1
        return encoded_obj

    def encode(self) -> bytes:
        """Encode the response."""
        packet = struct.pack(
            ">BBB", self.sub_function_code, self.read_code, self.conformity
        )
        objects = b""
        try:
            for object_id, data in iter(self.information.items()):
                if isinstance(data, list):
                    for item in data:
                        objects += self._encode_object(object_id, item)
                else:
                    objects += self._encode_object(object_id, data)
        except _OutOfSpaceException as exc:
            self.next_object_id = exc.oid
            self.more_follows = MoreData.KEEP_READING

        packet += struct.pack(
            ">BBB", self.more_follows, self.next_object_id, self.number_of_objects
        )
        packet += objects
        return packet

    @classmethod
    def decode_sub_function_code(cls, data: bytes) -> int:
        """Decode sub function code (1 byte)."""
        return int(data[2])

    def decode(self, data: bytes) -> None:
        """Decode a the response."""
        params = struct.unpack(">BBBBBB", data[0:6])
        self.sub_function_code, self.read_code = params[0:2]
        self.conformity, self.more_follows = params[2:4]
        self.next_object_id, self.number_of_objects = params[4:6]
        count = 6  # skip the header information
        self.information.clear()

        while count < len(data):
            object_id, object_length = struct.unpack(">BB", data[count : count + 2])
            count += object_length + 2
            if object_id not in self.information:
                self.information[object_id] = data[count - object_length : count]
            elif isinstance(self.information[object_id], list):
                self.information[object_id].append(data[count - object_length : count])
            else:
                self.information[object_id] = [
                    self.information[object_id],
                    data[count - object_length : count],
                ]


class DeviceIdentificationCollector:
    """Collect and merge paged Read Device Identification responses.

    Some devices return the device identification in several pages, each
    pointing to the next object id to request. This class implements the
    merge and consistency rules shared by the sync and async clients, so
    callers do not have to hand-write the ``next_object_id`` loop.

    Objects are kept as raw bytes. Repeated objects with identical content
    are deduplicated, conflicting content raises. A device that keeps
    returning the same page or jumps back to an already requested object id
    is detected and terminated with an exception.
    """

    DEFAULT_MAX_PAGES = 32
    DEFAULT_MAX_BYTES = 0xFFFF

    _VALID_CONFORMITY = frozenset(
        (*range(0x01, 0x05), *range(0x81, 0x85))
    )

    def __init__(
        self,
        read_code: int = DeviceInformation.BASIC,
        object_id: int = 0x00,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        """Initialize the collector.

        :param read_code: The device information read code
        :param object_id: The object id to start reading from
        :param max_pages: Maximum number of pages to request
        :param max_bytes: Maximum total size of collected object data
        """
        self.read_code = read_code
        self.max_pages = max_pages
        self.max_bytes = max_bytes
        self.objects: dict[int, bytes] = {}
        self.conformity: int | None = None
        self.pages = 0
        self.total_bytes = 0
        self.done = False
        self._current_object_id = object_id
        self._requested_object_ids = {object_id}

    def next_request(self, dev_id: int = 1) -> ReadDeviceInformationRequest:
        """Build the request for the next page."""
        if self.pages >= self.max_pages:
            self._fail(f"Page limit {self.max_pages} exceeded", None)
        return ReadDeviceInformationRequest(
            self.read_code, self._current_object_id, dev_id=dev_id
        )

    def _fail(self, reason: str, response: ModbusPDU | None) -> NoReturn:
        raise DeviceIdentificationError(
            reason,
            objects=dict(self.objects),
            response=response,
        )

    def add_response(self, response: ModbusPDU) -> bool:
        """Merge one response page.

        :param response: The response page to merge
        :returns: True if another page must be requested
        :raises DeviceIdentificationError: On inconsistent or runaway devices
        """
        if not isinstance(response, ReadDeviceInformationResponse):
            self._fail(f"Unexpected response {response!r}", response)
        response = cast(ReadDeviceInformationResponse, response)
        self.pages += 1
        if response.read_code != self.read_code:
            self._fail(
                f"Read code {response.read_code} does not match requested {self.read_code}",
                response,
            )
        if response.more_follows not in (MoreData.NOTHING, MoreData.KEEP_READING):
            self._fail(
                f"Invalid more follows {response.more_follows:#x}", response
            )
        if self.conformity is None:
            if response.conformity not in self._VALID_CONFORMITY:
                self._fail(
                    f"Invalid conformity {response.conformity:#x}", response
                )
            self.conformity = response.conformity
        elif response.conformity != self.conformity:
            self._fail(
                f"Conformity changed from {self.conformity:#x} to {response.conformity:#x}",
                response,
            )
        self._merge_objects(response)
        if response.more_follows == MoreData.NOTHING:
            self.done = True
            return False
        next_object_id = response.next_object_id
        if (
            next_object_id <= self._current_object_id
            or next_object_id in self._requested_object_ids
        ):
            self._fail(
                f"Device does not advance, next object id {next_object_id:#x} "
                f"already requested",
                response,
            )
        self._requested_object_ids.add(next_object_id)
        self._current_object_id = next_object_id
        return True

    def _merge_objects(self, response: ReadDeviceInformationResponse) -> None:
        """Merge page objects, deduplicating identical repeats."""
        for object_id, value in response.information.items():
            if isinstance(value, list):
                if any(item != value[0] for item in value):
                    self._fail(
                        f"Conflicting values for object {object_id:#x} in one page",
                        response,
                    )
                value = value[0]
            data = bytes(value)
            if object_id in self.objects:
                if self.objects[object_id] != data:
                    self._fail(
                        f"Conflicting content for object {object_id:#x}", response
                    )
                continue
            self.total_bytes += len(data)
            if self.total_bytes > self.max_bytes:
                self._fail(
                    f"Byte limit {self.max_bytes} exceeded", response
                )
            self.objects[object_id] = data

    def result(self, dev_id: int = 1) -> ReadDeviceInformationResponse:
        """Build the merged response with all collected objects."""
        response = ReadDeviceInformationResponse(
            read_code=self.read_code,
            information=dict(self.objects),
            dev_id=dev_id,
        )
        response.conformity = self.conformity or 0x00
        response.more_follows = MoreData.NOTHING
        response.next_object_id = 0x00
        response.number_of_objects = len(self.objects)
        return response


DecodePDU.add_pdu(_EncapsulatedInterfaceTransport, _EncapsulatedInterfaceTransport)
DecodePDU.add_sub_pdu(ReadDeviceInformationRequest, ReadDeviceInformationResponse)
