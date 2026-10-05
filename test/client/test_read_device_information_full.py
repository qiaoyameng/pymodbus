"""Tests for the paged Read Device Identification client convenience entry.

The responses are fully scripted, so no real device is involved. In addition
to the collector level tests the sync and async clients share the very same
scenarios and merge rules.
"""

import asyncio
import struct

import pytest

from pymodbus.client import AsyncModbusTcpClient, ModbusTcpClient
from pymodbus.constants import DeviceInformation, ExcCodes, MoreData
from pymodbus.exceptions import DeviceInformationException
from pymodbus.pdu import ExceptionResponse
from pymodbus.pdu.mei_message import (
    DeviceInformationCollector,
    ReadDeviceInformationRequest,
    ReadDeviceInformationResponse,
)


def build_page(
    read_code=DeviceInformation.BASIC,
    conformity=0x83,
    more_follows=MoreData.NOTHING,
    next_object_id=0,
    objects=None,
):
    """Build a decoded ReadDeviceInformationResponse from raw object bytes."""
    objects = objects or []
    data = struct.pack(
        ">BBBBBB",
        0x0E,
        read_code,
        conformity,
        more_follows,
        next_object_id,
        len(objects),
    )
    for object_id, value in objects:
        data += struct.pack(">BB", object_id, len(value)) + value
    response = ReadDeviceInformationResponse()
    response.decode(data)
    return response


def page_three():
    """Script a standard three page response sequence."""
    return [
        build_page(
            more_follows=MoreData.KEEP_READING,
            next_object_id=2,
            objects=[(0, b"Vendor"), (1, b"Product")],
        ),
        build_page(
            more_follows=MoreData.KEEP_READING,
            next_object_id=4,
            objects=[(2, b"v1.0"), (3, b"http://example.com")],
        ),
        build_page(
            more_follows=MoreData.NOTHING,
            next_object_id=0,
            objects=[(4, b"Name")],
        ),
    ]


def page_mutual_reference():
    """Script two pages whose next_object_id point back at each other."""
    return [
        build_page(
            more_follows=MoreData.KEEP_READING,
            next_object_id=2,
            objects=[(0, b"a")],
        ),
        build_page(
            more_follows=MoreData.KEEP_READING,
            next_object_id=0,
            objects=[(2, b"b")],
        ),
    ]


def page_repeated():
    """Script a device that repeats the same page."""
    repeated = build_page(
        more_follows=MoreData.KEEP_READING,
        next_object_id=2,
        objects=[(0, b"a"), (1, b"b")],
    )
    return [repeated, repeated]


class TestDeviceInformationCollector:
    """Collector merge/validation rules (transport independent)."""

    def test_three_pages_merged(self):
        """Three pages merge to one response with raw object bytes."""
        collector = DeviceInformationCollector(DeviceInformation.BASIC)
        for page in page_three()[:-1]:
            assert collector.add_page(page) is False
        assert collector.add_page(page_three()[-1]) is True

        result = collector.build_response()
        assert result.information == {
            0: b"Vendor",
            1: b"Product",
            2: b"v1.0",
            3: b"http://example.com",
            4: b"Name",
        }
        assert result.more_follows == MoreData.NOTHING
        assert result.next_object_id == 0
        assert result.conformity == 0x83
        assert result.number_of_objects == 5
        assert collector.pages == 3
        assert collector.complete

    def test_requests_use_next_object_id(self):
        """The collector drives requests with next_object_id."""
        requested = []

        collector = DeviceInformationCollector(DeviceInformation.BASIC)
        for page in page_three():
            requested.append(collector.next_object_id)
            collector.add_page(page)

        assert requested == [0, 2, 4]

    def test_mutual_reference_terminates(self):
        """Two pages pointing back at each other terminate the collection."""
        pages = page_mutual_reference()
        collector = DeviceInformationCollector(DeviceInformation.BASIC)
        collector.add_page(pages[0])
        with pytest.raises(DeviceInformationException) as exc:
            collector.add_page(pages[1])
        assert exc.value.failed_page is pages[1]
        assert exc.value.information == {0: b"a", 2: b"b"}
        assert not collector.complete

    def test_repeated_page_terminates(self):
        """A device that repeats the same page terminates the collection."""
        pages = page_repeated()
        collector = DeviceInformationCollector(DeviceInformation.BASIC)
        collector.add_page(pages[0])
        with pytest.raises(DeviceInformationException) as exc:
            collector.add_page(pages[1])
        assert exc.value.information == {0: b"a", 1: b"b"}
        assert exc.value.failed_page is pages[1]

    def test_duplicate_same_content_is_deduplicated(self):
        """Overlapping objects with identical bytes are merged."""
        collector = DeviceInformationCollector(DeviceInformation.BASIC)
        collector.add_page(
            build_page(
                more_follows=MoreData.KEEP_READING,
                next_object_id=2,
                objects=[(0, b"a"), (1, b"b")],
            )
        )
        collector.add_page(
            build_page(
                more_follows=MoreData.NOTHING,
                next_object_id=0,
                objects=[(1, b"b"), (2, b"c")],
            )
        )
        assert collector.information == {0: b"a", 1: b"b", 2: b"c"}

    def test_duplicate_different_content_fails(self):
        """The same object id with different bytes fails."""
        collector = DeviceInformationCollector(DeviceInformation.BASIC)
        collector.add_page(
            build_page(
                more_follows=MoreData.KEEP_READING,
                next_object_id=2,
                objects=[(0, b"a"), (1, b"b")],
            )
        )
        with pytest.raises(DeviceInformationException) as exc:
            collector.add_page(
                build_page(
                    more_follows=MoreData.KEEP_READING,
                    next_object_id=3,
                    objects=[(1, b"DIFFERENT"), (2, b"c")],
                )
            )
        assert exc.value.information == {0: b"a", 1: b"b"}

    def test_conformity_must_be_stable(self):
        """A changing conformity level fails."""
        collector = DeviceInformationCollector(DeviceInformation.BASIC)
        collector.add_page(
            build_page(
                conformity=0x83,
                more_follows=MoreData.KEEP_READING,
                next_object_id=1,
                objects=[(0, b"a")],
            )
        )
        with pytest.raises(DeviceInformationException):
            collector.add_page(
                build_page(
                    conformity=0x81,
                    more_follows=MoreData.NOTHING,
                    objects=[(1, b"b")],
                )
            )

    def test_read_code_must_match(self):
        """A response echoing a different read code fails."""
        collector = DeviceInformationCollector(DeviceInformation.REGULAR)
        with pytest.raises(DeviceInformationException):
            collector.add_page(build_page(read_code=DeviceInformation.BASIC))

    def test_more_follows_and_next_id_consistency(self):
        """more_follows=nothing requires next_object_id=0."""
        collector = DeviceInformationCollector(DeviceInformation.BASIC)
        with pytest.raises(DeviceInformationException):
            collector.add_page(
                build_page(
                    more_follows=MoreData.NOTHING,
                    next_object_id=3,
                    objects=[(0, b"a")],
                )
            )

    def test_invalid_more_follows(self):
        """A reserved more_follows value fails."""
        with pytest.raises(DeviceInformationException):
            DeviceInformationCollector(DeviceInformation.BASIC).add_page(
                build_page(more_follows=0x05, objects=[(0, b"a")])
            )

    def test_number_of_objects_must_match(self):
        """The declared object count must match the decoded objects."""
        data = struct.pack(">BBBBBB", 0x0E, 1, 0x83, 0, 0, 2)
        data += struct.pack(">BB", 0, 1) + b"a"
        page = ReadDeviceInformationResponse()
        page.decode(data)
        with pytest.raises(DeviceInformationException):
            DeviceInformationCollector(DeviceInformation.BASIC).add_page(page)

    def test_exception_page_fails(self):
        """An exception response fails and is attached to the exception."""
        page = ExceptionResponse(0x2B, ExcCodes.DEVICE_FAILURE)
        with pytest.raises(DeviceInformationException) as exc:
            DeviceInformationCollector(DeviceInformation.BASIC).add_page(page)
        assert exc.value.failed_page is page
        assert exc.value.information == {}

    def test_max_pages_limit(self):
        """The page limit terminates further polling."""
        collector = DeviceInformationCollector(DeviceInformation.BASIC, max_pages=2)
        collector.add_page(
            build_page(
                more_follows=MoreData.KEEP_READING,
                next_object_id=1,
                objects=[(0, b"a")],
            )
        )
        with pytest.raises(DeviceInformationException) as exc:
            collector.add_page(
                build_page(
                    more_follows=MoreData.KEEP_READING,
                    next_object_id=2,
                    objects=[(1, b"b")],
                )
            )
        assert exc.value.information == {0: b"a", 1: b"b"}

    def test_max_bytes_limit(self):
        """The total byte limit is enforced."""
        collector = DeviceInformationCollector(DeviceInformation.BASIC, max_bytes=3)
        with pytest.raises(DeviceInformationException) as exc:
            collector.add_page(build_page(objects=[(0, b"abcd")]))
        assert exc.value.information == {0: b"abcd"}

    def test_invalid_constructor_arguments(self):
        """Bad read codes and limits are rejected up front."""
        with pytest.raises(DeviceInformationException):
            DeviceInformationCollector(DeviceInformation.SPECIFIC)
        with pytest.raises(DeviceInformationException):
            DeviceInformationCollector(DeviceInformation.BASIC, max_pages=0)
        with pytest.raises(DeviceInformationException):
            DeviceInformationCollector(DeviceInformation.BASIC, max_bytes=-1)

    def test_repeated_object_within_page(self):
        """An object repeated within one page is flattened from the list form."""
        data = struct.pack(">BBBBBB", 0x0E, 1, 0x83, 0xFF, 3, 2)
        data += struct.pack(">BB", 2, 1) + b"c"
        data += struct.pack(">BB", 2, 1) + b"c"
        page = ReadDeviceInformationResponse()
        page.decode(data)

        collector = DeviceInformationCollector(DeviceInformation.BASIC)
        collector.add_page(page)
        assert collector.information == {2: b"c"}
        assert collector.next_object_id == 3

    def test_repeated_object_within_page_conflict(self):
        """Different bytes for an id within one page fails the collection."""
        data = struct.pack(">BBBBBB", 0x0E, 1, 0x83, 0, 0, 2)
        data += struct.pack(">BB", 0, 1) + b"a"
        data += struct.pack(">BB", 0, 2) + b"ab"
        page = ReadDeviceInformationResponse()
        page.decode(data)

        with pytest.raises(DeviceInformationException) as exc:
            DeviceInformationCollector(DeviceInformation.BASIC).add_page(page)
        assert exc.value.failed_page is page


class TestClientEntry:
    """Sync and async clients share the same merge rules."""

    def _make_clients(self):
        """Return (sync client, async client)."""
        return (
            ModbusTcpClient("127.0.0.1"),
            AsyncModbusTcpClient("127.0.0.1"),
        )

    def _script(self, mocker, client, pages):
        """Return (execute mock, requests seen) serving scripted pages."""
        seen = []

        def side_effect(_no_response_expected, request):
            seen.append(request)
            return pages[len(seen) - 1]

        async def async_side_effect(_no_response_expected, request):
            return side_effect(_no_response_expected, request)

        mock = mocker.patch.object(
            client,
            "execute",
            side_effect=async_side_effect
            if isinstance(client, AsyncModbusTcpClient)
            else side_effect,
        )
        return mock, seen

    async def _run(self, client, *args, **kwargs):
        """Call the convenience entry on either client type."""
        result = client.read_device_information_full(*args, **kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result

    @pytest.mark.parametrize("use_async", [False, True])
    async def test_three_pages(self, mocker, use_async):
        """A normal three page sequence is collected completely."""
        sync_client, async_client = self._make_clients()
        client = async_client if use_async else sync_client
        pages = page_three()
        _, seen = self._script(mocker, client, pages)

        result = await self._run(client)

        assert result.information == {
            0: b"Vendor",
            1: b"Product",
            2: b"v1.0",
            3: b"http://example.com",
            4: b"Name",
        }
        assert result.more_follows == MoreData.NOTHING
        assert len(seen) == 3
        assert [request.object_id for request in seen] == [0, 2, 4]
        assert all(
            isinstance(request, ReadDeviceInformationRequest) for request in seen
        )
        sync_client.close()
        async_client.close()

    @pytest.mark.parametrize("use_async", [False, True])
    async def test_mutual_reference_pages(self, mocker, use_async):
        """Two pages pointing at each other terminate, never polling forever."""
        sync_client, async_client = self._make_clients()
        client = async_client if use_async else sync_client
        pages = page_mutual_reference()
        mock, seen = self._script(mocker, client, pages)

        with pytest.raises(DeviceInformationException) as exc:
            await self._run(client)

        assert len(seen) == 2
        assert mock.call_count == 2
        assert exc.value.failed_page is pages[1]
        assert exc.value.information == {0: b"a", 2: b"b"}
        sync_client.close()
        async_client.close()

    @pytest.mark.parametrize("use_async", [False, True])
    async def test_repeated_pages(self, mocker, use_async):
        """A device repeating the same page terminates."""
        sync_client, async_client = self._make_clients()
        client = async_client if use_async else sync_client
        pages = page_repeated()
        mock, seen = self._script(mocker, client, pages)

        with pytest.raises(DeviceInformationException):
            await self._run(client)

        assert len(seen) == 2
        assert mock.call_count == 2
        sync_client.close()
        async_client.close()

    @pytest.mark.parametrize("use_async", [False, True])
    async def test_max_pages_limit(self, mocker, use_async):
        """The page limit terminates polling and keeps collected objects."""
        sync_client, async_client = self._make_clients()
        client = async_client if use_async else sync_client
        pages = page_three()
        _, seen = self._script(mocker, client, pages)

        with pytest.raises(DeviceInformationException) as exc:
            await self._run(client, max_pages=2)
        assert len(seen) == 2
        assert exc.value.information == {
            0: b"Vendor",
            1: b"Product",
            2: b"v1.0",
            3: b"http://example.com",
        }
        sync_client.close()
        async_client.close()

    @pytest.mark.parametrize("use_async", [False, True])
    async def test_single_page(self, mocker, use_async):
        """A single complete page needs exactly one request."""
        sync_client, async_client = self._make_clients()
        client = async_client if use_async else sync_client
        pages = [
            build_page(
                more_follows=MoreData.NOTHING,
                next_object_id=0,
                objects=[(0, b"Vendor"), (1, b"Product"), (2, b"v1.0")],
            )
        ]
        _, seen = self._script(mocker, client, pages)

        result = await self._run(client, read_code=DeviceInformation.BASIC)

        assert result.information == {
            0: b"Vendor",
            1: b"Product",
            2: b"v1.0",
        }
        assert len(seen) == 1
        sync_client.close()
        async_client.close()
