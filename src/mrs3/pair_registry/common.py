"""Small shared helpers of the pair registry."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .store import RegistryError


def decimal_text(value: object) -> str | None:
    """Canonical decimal text (`25.00` -> `25`, `0.0002` -> `0.0002`); empty or missing -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise RegistryError(f"not a number: {value!r}")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise RegistryError(f"not a number: {value!r}") from error
    if not number.is_finite():
        raise RegistryError(f"not a finite number: {value!r}")
    text = format(number.normalize(), "f")
    return "0" if text in {"-0", "0"} else text


def integer(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise RegistryError(f"not an integer: {value!r}")
    try:
        return int(str(value).strip())
    except ValueError as error:
        raise RegistryError(f"not an integer: {value!r}") from error
