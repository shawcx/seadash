"""`enum`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from dataclasses import dataclass

from ..builtins import CallContext, DecoratorName, Function, MODULES, Module
from ..types import Type


@dataclass
class EnumBase:
    """enum.Enum, IntEnum, StrEnum, Flag, IntFlag: a class inheriting one is an enum."""

    name: str


def enum_auto(ctx: CallContext) -> Type:
    raise ctx.error("auto() can only be the value of a member in an enum's body (`RED = auto()`)", ctx.call)


# Enums: the checker builds each one from its class body (Checker.resolve_enum_members).
MODULES["enum"] = Module("enum", {
    **{name: EnumBase(name) for name in ("Enum", "IntEnum", "StrEnum", "Flag", "IntFlag")},
    "auto": Function("auto", enum_auto),
    "unique": DecoratorName("unique"),
}, "modules/enum.hpp")
