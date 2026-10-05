"""Read transaction handling for datastore requests."""

from __future__ import annotations

from collections.abc import Callable
import inspect

from ..constants import ExcCodes
from ..datastore import ModbusServerContext
from .exceptionresponse import ExceptionResponse
from .pdu import ModbusPDU


async def read_datastore(
    context: ModbusServerContext,
    device_id: int,
    function_code: int,
    address: int,
    count: int,
    build_response: Callable[[list[int] | list[bool]], ModbusPDU],
) -> ModbusPDU:
    """Read a snapshot and commit after successful response construction."""
    signature = inspect.signature(context.async_getValues)
    supports_transaction = "transaction" in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if supports_transaction:
        values = await context.async_getValues(
            device_id, function_code, address, count, transaction=True
        )
    else:
        values = await context.async_getValues(device_id, function_code, address, count)
    if isinstance(values, ExcCodes):
        return ExceptionResponse(function_code, values)
    try:
        response = build_response(values)
    except Exception:
        rollback = getattr(values, "rollback", None)
        if rollback:
            await rollback()
        raise
    commit = getattr(values, "commit", None)
    if commit:
        await commit()
    return response
