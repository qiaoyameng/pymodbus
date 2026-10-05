"""Test paged Read Device Identification collection."""

import pytest

import pymodbus.client as lib_client
from pymodbus.constants import DeviceInformation, MoreData
from pymodbus.exceptions import DeviceIdentificationError
from pymodbus.pdu.mei_message import (
    DeviceIdentificationCollector,
    ReadDeviceInformationResponse,
)


def make_page(
    information,
    *,
    more_follows=MoreData.NOTHING,
    next_object_id=0x00,
    conformity=0x83,
    read_code=DeviceInformation.BASIC,
):
    """Build a scripted response page."""
    page = ReadDeviceInformationResponse(read_code=read_code, information=information)
    page.more_follows = more_follows
    page.next_object_id = next_object_id
    page.conformity = conformity
    page.number_of_objects = len(information)
    return page


class Script:
    """Play scripted response pages keyed by requested object id."""

    def __init__(self, pages):
        """Initialize with a dict of object_id -> page or callable."""
        self.pages = pages
        self.requested = []

    def __call__(self, _no_response_expected, request):
        self.requested.append(request.object_id)
        page = self.pages[request.object_id]
        return page(request) if callable(page) else page


def three_page_script():
    """Build a normal three page script."""
    return Script(
        {
            0x00: make_page(
                {0x00: b"Vendor", 0x01: b"Product"},
                more_follows=MoreData.KEEP_READING,
                next_object_id=0x02,
            ),
            0x02: make_page(
                {0x02: b"V1.0"},
                more_follows=MoreData.KEEP_READING,
                next_object_id=0x80,
            ),
            0x80: make_page({0x80: b"Private"}),
        }
    )


class TestReadDeviceIdentification:
    """Test the sync and async convenience entry points."""

    def test_sync_three_pages(self):
        """Collect three pages with the sync client."""
        client = lib_client.ModbusTcpClient("127.0.0.1")
        script = three_page_script()
        client.execute = script
        result = client.read_device_identification()
        assert result.information == {
            0x00: b"Vendor",
            0x01: b"Product",
            0x02: b"V1.0",
            0x80: b"Private",
        }
        assert result.more_follows == MoreData.NOTHING
        assert result.number_of_objects == 4
        assert result.conformity == 0x83
        assert script.requested == [0x00, 0x02, 0x80]

    async def test_async_three_pages(self):
        """Collect three pages with the async client."""
        client = lib_client.AsyncModbusTcpClient("127.0.0.1")
        script = three_page_script()

        async def execute(no_response_expected, request):
            return script(no_response_expected, request)

        client.execute = execute
        result = await client.read_device_identification()
        assert result.information == {
            0x00: b"Vendor",
            0x01: b"Product",
            0x02: b"V1.0",
            0x80: b"Private",
        }
        assert script.requested == [0x00, 0x02, 0x80]

    def test_sync_ping_pong_pages_terminate(self):
        """Two pages pointing back at each other must terminate."""
        client = lib_client.ModbusTcpClient("127.0.0.1")
        script = Script(
            {
                0x00: make_page(
                    {0x00: b"Vendor"},
                    more_follows=MoreData.KEEP_READING,
                    next_object_id=0x02,
                ),
                0x02: make_page(
                    {0x02: b"V1.0"},
                    more_follows=MoreData.KEEP_READING,
                    next_object_id=0x00,
                ),
            }
        )
        client.execute = script
        with pytest.raises(DeviceIdentificationError) as excinfo:
            client.read_device_identification()
        assert script.requested == [0x00, 0x02]
        assert excinfo.value.objects == {0x00: b"Vendor", 0x02: b"V1.0"}
        assert excinfo.value.response.next_object_id == 0x00

    async def test_async_repeated_page_terminate(self):
        """A device returning the same page forever must terminate."""
        client = lib_client.AsyncModbusTcpClient("127.0.0.1")
        page = make_page(
            {0x00: b"Vendor"},
            more_follows=MoreData.KEEP_READING,
            next_object_id=0x02,
        )
        script = Script({0x00: page, 0x02: page})

        async def execute(no_response_expected, request):
            return script(no_response_expected, request)

        client.execute = execute
        with pytest.raises(DeviceIdentificationError) as excinfo:
            await client.read_device_identification()
        assert script.requested == [0x00, 0x02]
        assert excinfo.value.objects == {0x00: b"Vendor"}

    def test_duplicate_identical_object_deduped(self):
        """Repeated object with identical content is deduplicated."""
        client = lib_client.ModbusTcpClient("127.0.0.1")
        script = Script(
            {
                0x00: make_page(
                    {0x00: b"Vendor"},
                    more_follows=MoreData.KEEP_READING,
                    next_object_id=0x01,
                ),
                0x01: make_page({0x00: b"Vendor", 0x01: b"Product"}),
            }
        )
        client.execute = script
        result = client.read_device_identification()
        assert result.information == {0x00: b"Vendor", 0x01: b"Product"}

    def test_duplicate_conflicting_object_fails(self):
        """Repeated object with different content must fail."""
        client = lib_client.ModbusTcpClient("127.0.0.1")
        script = Script(
            {
                0x00: make_page(
                    {0x00: b"Vendor"},
                    more_follows=MoreData.KEEP_READING,
                    next_object_id=0x01,
                ),
                0x01: make_page({0x00: b"Other", 0x01: b"Product"}),
            }
        )
        client.execute = script
        with pytest.raises(DeviceIdentificationError) as excinfo:
            client.read_device_identification()
        assert excinfo.value.objects == {0x00: b"Vendor"}
        assert excinfo.value.response.information[0x00] == b"Other"

    def test_max_pages_limit(self):
        """Page limit stops runaway devices."""
        client = lib_client.ModbusTcpClient("127.0.0.1")
        script = three_page_script()
        client.execute = script
        with pytest.raises(DeviceIdentificationError) as excinfo:
            client.read_device_identification(max_pages=2)
        assert script.requested == [0x00, 0x02]
        assert excinfo.value.objects == {
            0x00: b"Vendor",
            0x01: b"Product",
            0x02: b"V1.0",
        }

    def test_max_bytes_limit(self):
        """Byte limit stops oversized identifications."""
        client = lib_client.ModbusTcpClient("127.0.0.1")
        script = three_page_script()
        client.execute = script
        with pytest.raises(DeviceIdentificationError) as excinfo:
            client.read_device_identification(max_bytes=8)
        assert sum(len(v) for v in excinfo.value.objects.values()) <= 8

    def test_conformity_change_fails(self):
        """Conformity level must be consistent across pages."""
        client = lib_client.ModbusTcpClient("127.0.0.1")
        script = Script(
            {
                0x00: make_page(
                    {0x00: b"Vendor"},
                    more_follows=MoreData.KEEP_READING,
                    next_object_id=0x01,
                ),
                0x01: make_page({0x01: b"Product"}, conformity=0x81),
            }
        )
        client.execute = script
        with pytest.raises(DeviceIdentificationError):
            client.read_device_identification()

    def test_invalid_more_follows_fails(self):
        """An unknown more follows value must fail."""
        client = lib_client.ModbusTcpClient("127.0.0.1")
        script = Script({0x00: make_page({0x00: b"Vendor"}, more_follows=0x7F)})
        client.execute = script
        with pytest.raises(DeviceIdentificationError):
            client.read_device_identification()

    def test_unexpected_response_fails(self):
        """A non identification response must fail."""
        client = lib_client.ModbusTcpClient("127.0.0.1")
        script = Script({0x00: object()})
        client.execute = script
        with pytest.raises(DeviceIdentificationError) as excinfo:
            client.read_device_identification()
        assert excinfo.value.objects == {}


class TestDeviceIdentificationCollector:
    """Test the shared merge rules directly."""

    def test_result_preserves_raw_bytes(self):
        """Collected objects keep their raw bytes."""
        collector = DeviceIdentificationCollector(DeviceInformation.REGULAR)
        assert collector.add_response(
            make_page({0x00: b"\x00\xffbinary"}, read_code=DeviceInformation.REGULAR)
        ) is False
        result = collector.result()
        assert result.information[0x00] == b"\x00\xffbinary"
        assert result.more_follows == MoreData.NOTHING

    def test_read_code_mismatch_fails(self):
        """A page with another read code must fail."""
        collector = DeviceIdentificationCollector(DeviceInformation.BASIC)
        with pytest.raises(DeviceIdentificationError):
            collector.add_response(
                make_page({0x00: b"Vendor"}, read_code=DeviceInformation.REGULAR)
            )

    def test_invalid_conformity_fails(self):
        """An unknown conformity level must fail."""
        collector = DeviceIdentificationCollector()
        with pytest.raises(DeviceIdentificationError):
            collector.add_response(make_page({0x00: b"Vendor"}, conformity=0x10))

    def test_conflicting_values_within_page_fail(self):
        """Conflicting duplicate values inside one page must fail."""
        collector = DeviceIdentificationCollector()
        page = make_page({0x00: [b"one", b"two"]})
        with pytest.raises(DeviceIdentificationError):
            collector.add_response(page)
