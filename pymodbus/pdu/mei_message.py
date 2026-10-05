"""Encapsulated Interface (MEI) Transport Messages."""

from __future__ import annotations

import struct
from typing import Any

from ..constants import DeviceInformation, ExcCodes, MoreData
from ..datastore import ModbusServerContext
from ..exceptions import DeviceInformationException
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


class DeviceInformationCollector:
    """Collect and validate all pages of a Read Device Identification reply.

    The class is independent of the transport and used by both the synchronous
    and asynchronous clients. The caller performs the actual requests using the
    :attr:`next_object_id` while the collector merges each response page and
    verifies that the pages are self-consistent and make forward progress.

    Object values are always kept as the raw bytes returned by the device.
    Duplicate objects containing identical bytes are de-duplicated; if a device
    returns different bytes for the same object id the collection fails.
    """

    def __init__(
        self,
        read_code: int,
        *,
        max_pages: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        """Initialize a new collector.

        :param read_code: The device information read code (basic/regular/extended)
        :param max_pages: Maximum number of pages accepted
        :param max_bytes: Maximum total number of object bytes accepted
        """
        if not DeviceInformation.BASIC <= read_code <= DeviceInformation.EXTENDED:
            raise DeviceInformationException(
                f"Invalid read_code {read_code}, expected 1..3."
            )
        if max_pages is not None and max_pages < 1:
            raise DeviceInformationException("max_pages must be >= 1.")
        if max_bytes is not None and max_bytes < 0:
            raise DeviceInformationException("max_bytes must be >= 0.")
        self.read_code = read_code
        self.max_pages = max_pages
        self.max_bytes = max_bytes
        self.next_object_id = 0x00
        self.pages = 0
        self.information: dict[int, bytes] = {}
        self.conformity: int | None = None
        self.complete = False
        self._total_bytes = 0

    @staticmethod
    def _iter_objects(page: ReadDeviceInformationResponse):
        """Yield (object id, raw bytes) pairs contained in a response page."""
        for object_id, value in page.information.items():
            if isinstance(value, list):
                for item in value:
                    yield object_id, item
            else:
                yield object_id, value

    def _raise(self, text: str, page: ModbusPDU) -> None:
        """Raise with the objects collected so far and the failing page."""
        raise DeviceInformationException(
            text, information=self.information, failed_page=page
        )

    def add_page(self, page: ModbusPDU) -> bool:
        """Merge one response page.

        :param page: A decoded ``ReadDeviceInformationResponse`` or error PDU
        :returns: ``True`` when all pages are collected, ``False`` when another
                  page must be requested (use :attr:`next_object_id`)
        :raises DeviceInformationException: when the page is an error or the
                  pages are not self-consistent or do not progress
        """
        if not isinstance(page, ReadDeviceInformationResponse):
            self._raise(
                "Unexpected response for Read Device Identification.", page
            )
        if page.isError():
            self._raise("Device returned an error response.", page)
        if page.read_code != self.read_code:
            self._raise(
                f"Response read_code {page.read_code} does not match "
                f"requested read_code {self.read_code}.",
                page,
            )
        if page.more_follows not in (MoreData.NOTHING, MoreData.KEEP_READING):
            self._raise(
                f"Invalid more_follows value {page.more_follows}.", page
            )
        if self.conformity is not None and page.conformity != self.conformity:
            self._raise(
                f"Conformity level changed from {self.conformity} "
                f"to {page.conformity}.",
                page,
            )
        self.conformity = page.conformity

        objects = list(self._iter_objects(page))
        if len(objects) != page.number_of_objects:
            self._raise(
                f"Page declares {page.number_of_objects} objects but "
                f"contains {len(objects)}.",
                page,
            )

        for object_id, value in objects:
            if object_id in self.information:
                if self.information[object_id] != value:
                    self._raise(
                        f"Conflicting content received for object id "
                        f"{object_id}.",
                        page,
                    )
            else:
                self.information[object_id] = value

        self.pages += 1
        self._total_bytes = sum(len(value) for value in self.information.values())
        if self.max_pages is not None and (
            page.more_follows == MoreData.KEEP_READING
            and self.pages >= self.max_pages
        ):
            self._raise(
                f"More than {self.max_pages} response pages required.", page
            )
        if self.max_bytes is not None and self._total_bytes > self.max_bytes:
            self._raise(
                f"Collected {self._total_bytes} object bytes, exceeding the "
                f"limit of {self.max_bytes}.",
                page,
            )

        if page.more_follows == MoreData.NOTHING:
            if page.next_object_id != 0x00:
                self._raise(
                    f"more_follows=nothing but next_object_id is "
                    f"{page.next_object_id} (expected 0).",
                    page,
                )
            self.next_object_id = 0x00
            self.complete = True
            return True

        if not objects or page.next_object_id <= objects[-1][0]:
            self._raise(
                f"Invalid next_object_id {page.next_object_id}; the device "
                f"does not progress past object id "
                f"{objects[-1][0] if objects else self.next_object_id - 1}.",
                page,
            )
        if page.next_object_id <= self.next_object_id:
            self._raise(
                f"next_object_id {page.next_object_id} jumps back to an "
                f"object id already requested ({self.next_object_id}).",
                page,
            )
        self.next_object_id = page.next_object_id
        return False

    def build_response(self) -> ReadDeviceInformationResponse:
        """Build the merged response containing all collected objects."""
        response = ReadDeviceInformationResponse(
            read_code=self.read_code,
            information=dict(self.information),
        )
        if self.conformity is not None:
            response.conformity = self.conformity
        response.number_of_objects = len(self.information)
        response.more_follows = MoreData.NOTHING
        response.next_object_id = 0x00
        return response


DecodePDU.add_pdu(_EncapsulatedInterfaceTransport, _EncapsulatedInterfaceTransport)
DecodePDU.add_sub_pdu(ReadDeviceInformationRequest, ReadDeviceInformationResponse)
