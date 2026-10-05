"""Pymodbus Exceptions.

Custom exceptions to be used in the Modbus code.
"""

__all__ = [
    "ConnectionException",
    "DeviceInformationException",
    "MessageRegisterException",
    "ModbusIOException",
    "NoSuchIdException",
    "NotImplementedException",
    "ParameterException",
]


class ModbusException(Exception):
    """Base modbus exception."""

    def __init__(self, string: str) -> None:
        """Initialize the exception.

        :param string: The message to append to the error
        """
        self.string = string
        super().__init__(string)

    def __str__(self) -> str:
        """Return string representation."""
        return f"Modbus Error: {self.string}"

    def isError(self) -> bool:
        """Error"""
        return True


class ModbusIOException(ModbusException):
    """Error resulting from data i/o."""

    def __init__(
        self,
        string: str = "",
        function_code: int | None = None,
        *,
        transaction_id: int = 0,
        dev_id: int = 0,
    ) -> None:
        """Initialize the exception.

        :param string: The message to append to the error
        :param function_code: Optional function code associated with the error
        :param transaction_id: Optional framing transaction id (e.g. MBAP TID)
        :param dev_id: Optional framing device / unit id
        """
        self.fcode = function_code
        self.transaction_id = transaction_id
        self.dev_id = dev_id
        self.message = f"[Input/Output] {string}"
        ModbusException.__init__(self, self.message)


class ParameterException(ModbusException):
    """Error resulting from invalid parameter."""

    def __init__(self, string: str = "") -> None:
        """Initialize the exception.

        :param string: The message to append to the error
        """
        message = f"[Invalid Parameter] {string}"
        ModbusException.__init__(self, message)


class NoSuchIdException(ModbusException):
    """Error resulting from making a request to a id that does not exist."""

    def __init__(self, string: str = "") -> None:
        """Initialize the exception.

        :param string: The message to append to the error
        """
        message = f"[No Such id] {string}"
        ModbusException.__init__(self, message)


class NotImplementedException(ModbusException):
    """Error resulting from not implemented function."""

    def __init__(self, string: str = "") -> None:
        """Initialize the exception.

        :param string: The message to append to the error
        """
        message = f"[Not Implemented] {string}"
        ModbusException.__init__(self, message)


class ConnectionException(ModbusException):
    """Error resulting from a bad connection."""

    def __init__(self, string: str = "") -> None:
        """Initialize the exception.

        :param string: The message to append to the error
        """
        message = f"[Connection] {string}"
        ModbusException.__init__(self, message)


class DeviceInformationException(ModbusException):
    """Error while collecting a multi-page Read Device Identification response.

    The exception carries the objects collected before the failure as well as
    the page (or exception response) that triggered the error, allowing the
    caller to inspect partial results.
    """

    def __init__(
        self,
        string: str = "",
        *,
        information: dict | None = None,
        failed_page: object | None = None,
    ) -> None:
        """Initialize the exception.

        :param string: The message to append to the error
        :param information: Objects collected before the failure
        :param failed_page: The response page that triggered the error
        """
        self.information: dict = dict(information or {})
        self.failed_page = failed_page
        message = f"[Device Information] {string}"
        ModbusException.__init__(self, message)


class MessageRegisterException(ModbusException):
    """Error resulting from failing to register a custom message request/response."""

    def __init__(self, string: str = "") -> None:
        """Initialize."""
        message = f"[Error registering message] {string}"
        ModbusException.__init__(self, message)
