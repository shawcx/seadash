"""`argparse`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from .. import ast as A
from ..builtins import (
    CallContext, Function, Module, module_type, MODULES, NamedType, signature, strip_optional_type, sync_method,
)
from ..types import (
    assignable, BOOL, FLOAT, INT, ListType, NamespaceType, NONE, OptionalType, PARSER, ParserType, PATH, STR,
    SubParsersType, Type,
)


ACTIONS = ("store", "store_true", "store_false", "store_const", "count", "append", "help", "version")
ARG_KEYWORDS = ("action", "nargs", "const", "default", "type", "choices", "required", "help", "metavar", "dest", "version")


def parser_key(ctx: CallContext) -> int:
    key = ctx.receiver.key
    if key is None:
        raise ctx.error(f"{ctx.what} needs a parser created in this module (so its arguments are known)")
    return key


def new_parser(ctx: CallContext) -> Type:
    """ArgumentParser(...): a parser of its own, for add_argument() to describe."""
    ARGUMENT_PARSER_SIGNATURE(ctx)
    return ParserType(id(ctx.call))


def parser_add_subparsers(ctx: CallContext) -> Type:
    key = parser_key(ctx)
    ctx.arity(0, keywords=("dest", "required", "title", "description", "help", "metavar"))
    kw = {k.name: k.value for k in ctx.call.keywords}
    dest = literal_str(ctx, kw["dest"], "dest=") if "dest" in kw else ""
    required = False
    if "required" in kw:
        if not isinstance(kw["required"], A.BoolLit):
            raise ctx.error("required= must be True or False written out", kw["required"])
        kw["required"].ty = BOOL
        required = kw["required"].value
    for name in ("title", "description", "help", "metavar"):
        if name in kw:
            ctx.checker.expect_type(kw[name], STR, name)
    if key in ctx.checker.subcommands:
        raise ctx.error("a parser can only have one add_subparsers()", ctx.call)
    ctx.checker.subcommands[key] = {"dest": dest, "required": required, "commands": []}
    ctx.call.notes["argparse_sub"] = {"dest": dest, "required": required, "kw": kw}
    return SubParsersType(key)


def subparsers_add_parser(ctx: CallContext) -> Type:
    parent = ctx.receiver.parent
    if not ctx.args:
        raise ctx.error("add_parser() needs the subcommand's name")
    ctx.arity(1, keywords=("help", "aliases", "description"))
    name = literal_str(ctx, ctx.args[0], "the subcommand's name")
    kw = {k.name: k.value for k in ctx.call.keywords}
    aliases: list[str] = []
    if "aliases" in kw:
        if not isinstance(kw["aliases"], (A.ListLit, A.TupleLit)):
            raise ctx.error("aliases= must be a list written out, like aliases=['rm']", kw["aliases"])
        aliases = [literal_str(ctx, a, "an alias") for a in kw["aliases"].elts]
        kw["aliases"].ty = ListType(STR)
    for field in ("help", "description"):
        if field in kw:
            ctx.checker.expect_type(kw[field], STR, field)
    commands = ctx.checker.subcommands[parent]["commands"]
    for names, _ in commands:
        if name in names or set(aliases) & set(names):
            raise ctx.error(f"conflicting subparser: {name}", ctx.call)
    child = ParserType(id(ctx.call))
    commands.append(((name, *aliases), child.key))
    ctx.call.notes["argparse_cmd"] = {"name": name, "aliases": aliases, "kw": kw}
    return child


def literal_str(ctx: CallContext, node: A.Expr, what: str) -> str:
    if not isinstance(node, A.StrLit):
        raise ctx.error(f"{what} must be a string written out (it decides the parsed type)", node)
    node.ty = STR
    return node.value


def argument_kind(ctx: CallContext, node: A.Expr | None) -> tuple[str, Type]:
    """type=int / float / str / Path -> (runtime kind, value type)."""
    if node is None:
        return "STR", STR
    node.notes["compile_time"] = True
    if isinstance(node, A.Name) and node.id in ("int", "float", "str") and not ctx.checker.state.names.get(node.id):
        return node.id.upper(), {"int": INT, "float": FLOAT, "str": STR}[node.id]
    if ctx.checker.builtin_class(node) == PATH or (
        isinstance(node, A.Name) and node.id in ctx.checker.imported
        and ctx.checker.imported[node.id][0] is MODULES["pathlib"]
    ):
        return "PATH", PATH
    raise ctx.error("type= must be int, float, str or Path (written out: it decides the parsed type)", node)


def parser_add_argument(ctx: CallContext) -> Type:
    var = parser_key(ctx)
    if not ctx.args:
        raise ctx.error("add_argument() needs a name, or option flags like '-v', '--verbose'")
    names = [literal_str(ctx, a, "an argument name") for a in ctx.args]
    kw: dict[str, A.Expr] = {}
    for k in ctx.call.keywords:
        if k.name not in ARG_KEYWORDS:
            raise ctx.error(f"add_argument() got an unexpected keyword argument '{k.name}'", k)
        kw[k.name] = k.value
    positional = not names[0].startswith("-")
    if positional and len(names) > 1:
        raise ctx.error("a positional argument has one name (options start with '-')", ctx.args[1])
    if not positional and any(not n.startswith("-") for n in names):
        raise ctx.error("option flags all start with '-'", ctx.call)
    action = literal_str(ctx, kw["action"], "action=") if "action" in kw else "store"
    if action not in ACTIONS:
        raise ctx.error(f"unknown action {action!r} (supported: {', '.join(ACTIONS)})", kw["action"])
    nargs = "ONE"
    if "nargs" in kw:
        n = kw["nargs"]
        if isinstance(n, A.IntLit):
            nargs = str(n.value)
        elif isinstance(n, A.StrLit) and n.value in ("?", "*", "+"):
            nargs = {"?": "OPTIONAL", "*": "ANY", "+": "SOME"}[n.value]
        else:
            raise ctx.error("nargs= must be a number, '?', '*' or '+' (written out)", n)
        n.ty = INT if isinstance(n, A.IntLit) else STR
    if "dest" in kw:
        dest = literal_str(ctx, kw["dest"], "dest=")
    elif positional:
        dest = names[0]
    else:
        long = next((n for n in names if n.startswith("--")), names[0])
        dest = long.lstrip("-").replace("-", "_")
    if positional and "required" in kw:
        raise ctx.error("'required' is an invalid argument for positionals", kw["required"])
    kind, item = argument_kind(ctx, kw.get("type"))
    required = False
    if "required" in kw:
        if not isinstance(kw["required"], A.BoolLit):
            raise ctx.error("required= must be True or False written out", kw["required"])
        kw["required"].ty = BOOL
        required = kw["required"].value
    many = nargs not in ("ONE", "OPTIONAL")
    has_default = "default" in kw and not isinstance(kw["default"], A.NoneLit)
    # What the attribute holds, following Python's rules for when it can be None.
    match action:
        case "store_true" | "store_false":
            t = BOOL
        case "count":
            t = INT if has_default else OptionalType(INT)
        case "append":
            t = ListType(item) if has_default else OptionalType(ListType(item))
        case "store_const":
            if "const" not in kw:
                raise ctx.error("action='store_const' needs const=", ctx.call)
            t = ctx.checker.check_expr(kw["const"])
            if not has_default:
                t = t if isinstance(t, OptionalType) else OptionalType(t)
        case "help" | "version":
            t = None
        case _:
            base = ListType(item) if many else item
            always = (positional and nargs in ("ONE", "SOME") or nargs.isdigit()) or (positional and nargs == "ANY")
            t = base if always or has_default or required else OptionalType(base)
    if action == "version" and "version" not in kw:
        raise ctx.error("action='version' needs version=", ctx.call)
    for name, want in (("help", STR), ("metavar", STR), ("version", STR)):
        if name in kw:
            ctx.checker.expect_type(kw[name], want, name)
    if has_default and t is not None:
        want = strip_optional_type(t)
        actual = ctx.checker.check_expr(kw["default"], want)
        if not assignable(actual, want):
            raise ctx.error(f"default= must be {want} for this argument, not {actual}", kw["default"])
    elif "default" in kw:
        ctx.checker.check_expr(kw["default"])
    if "const" in kw and action != "store_const":
        ctx.checker.expect_type(kw["const"], item, "const")
    if "choices" in kw:
        ch = kw["choices"]
        if not isinstance(ch, (A.ListLit, A.TupleLit)):
            raise ctx.error("choices= must be a list written out, like choices=['fast', 'slow']", ch)
        for c in ch.elts:
            ctx.checker.expect_type(c, item, "each choice")
        ch.ty = ListType(item)
    specs = ctx.checker.argument_parsers.setdefault(var, [])
    for existing, _ in specs:
        if existing == dest and t is not None:
            raise ctx.error(f"'{dest}' is already an argument of this parser", ctx.call)
    if t is not None:
        specs.append((dest, t))
    ctx.call.notes["argparse"] = {"flags": [] if positional else names, "dest": dest, "action": action, "nargs": nargs,
                         "kind": kind, "kw": kw, "required": required}
    return NONE


def parser_parse_args(ctx: CallContext) -> Type:
    key = parser_key(ctx)
    ctx.arity(0, 1, keywords=("args",))
    node = ctx.args[0] if ctx.args else ctx.keyword_arg("args")
    if node is not None and not isinstance(node, A.NoneLit):
        ctx.checker.expect_type(node, ListType(STR), "args")
    ctx.call.notes["parse_args"] = node  # (the bound argument, for codegen)
    fields = list(ctx.checker.argument_parsers.get(key, []))
    commands = []
    sub = ctx.checker.subcommands.get(key)
    if sub is not None:
        if sub["dest"]:
            fields.append((sub["dest"], STR if sub["required"] else OptionalType(STR)))
        merged: dict[str, Type] = {}
        for names, child in sub["commands"]:
            own = tuple(ctx.checker.argument_parsers.get(child, []))
            commands.append((sub["dest"], names, own))
            for name, t in own:
                opt = t if isinstance(t, OptionalType) else OptionalType(t)  # only there for that subcommand
                if any(name == f for f, _ in fields):
                    raise ctx.error(f"subcommand '{names[0]}' has an argument '{name}' that its parser already has")
                if name in merged and merged[name] != opt:
                    raise ctx.error(f"'{name}' is {merged[name]} in one subcommand and {opt} in another")
                merged[name] = opt
        fields.extend(merged.items())
    return NamespaceType(tuple(fields), tuple(commands))


PARSER_METHODS = {
    "add_argument": parser_add_argument,
    "add_subparsers": parser_add_subparsers,
    "parse_args": parser_parse_args,
    "print_help": sync_method(NONE),
    "print_usage": sync_method(NONE),
    "format_help": sync_method(STR),
    "format_usage": sync_method(STR),
    "error": sync_method(NONE, ("message", STR)),
    "exit": sync_method(NONE, ("status", INT, "0"), ("message", OptionalType(STR), "std::nullopt")),
}
ARGUMENT_PARSER_SIGNATURE = signature(
    PARSER, *((name, OptionalType(STR), "std::nullopt") for name in ("prog", "usage", "description", "epilog")),
    ("add_help", BOOL, "true"))
MODULES["argparse"] = Module("argparse", {
    "ArgumentParser": Function("ArgumentParser", new_parser, "sd::argparse::ArgumentParser", as_type=PARSER),
    "Namespace": NamedType("Namespace", NamespaceType()),
}, "modules/argparse.hpp")
MODULES["argparse"].members["ArgumentParser"].params = ARGUMENT_PARSER_SIGNATURE.params
module_type(ParserType, methods=PARSER_METHODS)
module_type(SubParsersType, methods={"add_parser": subparsers_add_parser})
module_type(NamespaceType, attributes=lambda t: {name: (lambda _, ft=ft: ft) for name, ft in t.fields})
