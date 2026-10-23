"""Bounded decimal input and canonical arithmetic for the additive Spot lane."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Context, Decimal, InvalidOperation, localcontext
from functools import wraps
from typing import Annotated

from pydantic import BeforeValidator, PlainSerializer


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def number(value):
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise ValueError("Spot number must be a decimal value")
    if len(str(value)) > 512:
        raise ValueError("oversized Spot decimal")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid Spot decimal") from exc
    if (
        not parsed.is_finite()
        or abs(parsed) > Decimal("1e100")
        or parsed.as_tuple().exponent < -300
    ):
        raise ValueError("invalid or oversized Spot decimal")
    return parsed


def input_number(value):
    if len(str(value)) > 128:
        raise ValueError("oversized Spot parameter")
    parsed = number(value)
    if parsed < 0 or parsed > Decimal("1e18") or parsed.as_tuple().exponent < -18:
        raise ValueError("Spot parameter outside supported bounds")
    return parsed


Number = Annotated[
    Decimal, BeforeValidator(number), PlainSerializer(decimal_text, when_used="json")
]
InputNumber = Annotated[
    Decimal, BeforeValidator(input_number), PlainSerializer(decimal_text, when_used="json")
]


def exact(function):
    @wraps(function)
    def call(*args, **kwargs):
        with localcontext(Context(prec=256)):
            return function(*args, **kwargs)

    return call


@exact
def floor_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


@exact
def ceil_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step
