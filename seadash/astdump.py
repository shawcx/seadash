"""Compact text rendering of syntax trees, for tests and `sd ast`.

Expressions become S-expressions so precedence is explicit:

    1 + 2 * -x      ->  (+ 1 (* 2 (- x)))
    f(a, k=1)[1:]   ->  (index (call f a k=1) (slice 1 _ _))

Statements are one per line; blocks are indented under a header line:

    def f(n: int) -> int:
      (return (* n 2))
"""

from . import ast as A


def dump(node: A.Module | list[A.Stmt]) -> str:
    body = node.body if isinstance(node, A.Module) else node
    return "\n".join(stmt_lines(body, 0))


def expr(e: A.Expr | None) -> str:
    match e:
        case None:
            return "_"
        case A.IntLit(v) | A.FloatLit(v) | A.StrLit(v) | A.BytesLit(v) | A.BoolLit(v):
            return repr(v)
        case A.NoneLit():
            return "None"
        case A.Name(id):
            return id
        case A.FString(parts):
            return sexp("fstr", *(repr(p) if isinstance(p, str) else fvalue(p) for p in parts))
        case A.ListLit(elts):
            return sexp("list", *map(expr, elts))
        case A.TupleLit(elts):
            return sexp("tuple", *map(expr, elts))
        case A.SetLit(elts):
            return sexp("set", *map(expr, elts))
        case A.DictLit(keys, values):
            return sexp("dict", *(f"{expr(k)}: {expr(v)}" for k, v in zip(keys, values)))
        case A.ListComp(elt, gens):
            return sexp("listcomp", expr(elt), *map(comp, gens))
        case A.SetComp(elt, gens):
            return sexp("setcomp", expr(elt), *map(comp, gens))
        case A.GeneratorExp(elt, gens):
            return sexp("genexp", expr(elt), *map(comp, gens))
        case A.DictComp(k, v, gens):
            return sexp("dictcomp", f"{expr(k)}: {expr(v)}", *map(comp, gens))
        case A.UnaryOp(op, operand):
            return sexp(op, expr(operand))
        case A.BinOp(op, left, right) | A.BoolOp(op, left, right):
            return sexp(op, expr(left), expr(right))
        case A.Compare(left, [op], [right]):
            return sexp(op, expr(left), expr(right))
        case A.Compare(left, ops, rights):
            chain = [expr(left)]
            for op, right in zip(ops, rights):
                chain += [op, expr(right)]
            return sexp("chain", *chain)
        case A.IfExp(test, body, orelse):
            return sexp("if-exp", expr(test), expr(body), expr(orelse))
        case A.Lambda(params, body):
            return sexp("lambda", "(" + " ".join(p.name for p in params) + ")", expr(body))
        case A.NamedExpr(target, value):
            return sexp(":=", target.id, expr(value))
        case A.Call(func, args, keywords):
            return sexp("call", expr(func), *map(expr, args), *(f"{k.name}={expr(k.value)}" for k in keywords))
        case A.Attribute(value, attr):
            return sexp(".", expr(value), attr)
        case A.Index(value, index):
            return sexp("index", expr(value), expr(index))
        case A.Slice(lower, upper, step):
            return sexp("slice", expr(lower), expr(upper), expr(step))
    raise TypeError(f"can't dump {e!r}")


def fvalue(fv: A.FormattedValue) -> str:
    spec = fv.spec
    if isinstance(spec, A.FString):
        spec = "".join(p if isinstance(p, str) else fvalue(p) for p in spec.parts)
    spec = f":{spec}" if spec is not None else ""
    conversion = f"!{fv.conversion}" if fv.conversion else ""
    return "{" + expr(fv.value) + conversion + spec + "}"


def comp(c: A.Comprehension) -> str:
    return sexp("for", expr(c.target), expr(c.iter), *(sexp("if", expr(i)) for i in c.ifs))


def sexp(head: str, *items: str) -> str:
    return "(" + " ".join([head, *items]) + ")"


def stmt_lines(body: list[A.Stmt], depth: int) -> list[str]:
    lines: list[str] = []
    for s in body:
        lines += stmt(s, depth)
    return lines


def stmt(s: A.Stmt, depth: int) -> list[str]:
    pad = "  " * depth

    def header(text: str, block: list[A.Stmt], orelse: list[A.Stmt] = ()) -> list[str]:
        out = [pad + text] + stmt_lines(block, depth + 1)
        if orelse:
            out += [pad + "else:"] + stmt_lines(orelse, depth + 1)
        return out

    match s:
        case A.ExprStmt(value):
            return [pad + sexp("expr", expr(value))]
        case A.Assign(targets, value):
            return [pad + sexp("=", *map(expr, targets), expr(value))]
        case A.AnnAssign(target, ann, value):
            return [pad + sexp(":", expr(target), str(ann), *([expr(value)] if value else []))]
        case A.AugAssign(target, op, value):
            return [pad + sexp(op + "=", expr(target), expr(value))]
        case A.Pass():
            return [pad + "(pass)"]
        case A.Break():
            return [pad + "(break)"]
        case A.Continue():
            return [pad + "(continue)"]
        case A.Return(value):
            return [pad + sexp("return", *([expr(value)] if value else []))]
        case A.Yield(value, from_):
            return [pad + sexp("yield-from" if from_ else "yield", *([expr(value)] if value else []))]
        case A.Assert(test, msg):
            return [pad + sexp("assert", expr(test), *([expr(msg)] if msg else []))]
        case A.Nonlocal(names):
            return [pad + sexp("nonlocal", *names)]
        case A.Global(names):
            return [pad + sexp("global", *names)]
        case A.Import(names):
            return [pad + sexp("import", *map(alias, names))]
        case A.ImportFrom(module, names):
            return [pad + sexp("from", module, "import", *map(alias, names))]
        case A.Raise(exc, cause):
            parts = ([expr(exc)] if exc else []) + (["from", expr(cause)] if cause else [])
            return [pad + sexp("raise", *parts)]
        case A.Try(body, handlers, orelse, finalbody):
            out = [pad + "try:"] + stmt_lines(body, depth + 1)
            for h in handlers:
                what = (" " + expr(h.type)) if h.type else ""
                what += f" as {h.name.id}" if h.name else ""
                out += [pad + f"except{what}:"] + stmt_lines(h.body, depth + 1)
            if orelse:
                out += [pad + "else:"] + stmt_lines(orelse, depth + 1)
            if finalbody:
                out += [pad + "finally:"] + stmt_lines(finalbody, depth + 1)
            return out
        case A.Match(subject, cases):
            out = [pad + f"match {expr(subject)}:"]
            for c in cases:
                guard = f" if {expr(c.guard)}" if c.guard else ""
                out += [pad + f"  case {pattern(c.pattern)}{guard}:"] + stmt_lines(c.body, depth + 2)
            return out
        case A.With(items, body):
            parts = [expr(i.context) + (f" as {expr(i.target)}" if i.target else "") for i in items]
            return header(f"with {', '.join(parts)}:", body)
        case A.If(test, body, orelse):
            return header(f"if {expr(test)}:", body, orelse)
        case A.While(test, body, orelse):
            return header(f"while {expr(test)}:", body, orelse)
        case A.For(target, it, body, orelse):
            return header(f"for {expr(target)} in {expr(it)}:", body, orelse)
        case A.FunctionDef(name, params, returns, body, type_params, decorators):
            ret = f" -> {returns}" if returns else ""
            decos = [pad + f"@{expr(d)}" for d in decorators]
            return decos + header(f"def {name}{tparams(type_params)}({', '.join(map(param, params))}){ret}:", body)
        case A.ClassDef(kind, name, bases, body, type_params, decorators):
            base_text = f"({', '.join(map(str, bases))})" if bases else ""
            decos = [pad + f"@{expr(d)}" for d in decorators]
            return decos + header(f"{kind} {name}{tparams(type_params)}{base_text}:", body)
    raise TypeError(f"can't dump {s!r}")


def pattern(p: A.Pattern) -> str:
    match p:
        case A.MatchValue(value):
            return expr(value)
        case A.MatchAs(None, None):
            return "_"
        case A.MatchAs(None, name):
            return name.id
        case A.MatchAs(inner, name):
            return f"({pattern(inner)} as {name.id})"
        case A.MatchOr(options):
            return "(" + " | ".join(map(pattern, options)) + ")"
        case A.MatchSequence(items):
            return "[" + ", ".join(map(pattern, items)) + "]"
        case A.MatchStar(name):
            return "*" + (name.id if name else "_")
        case A.MatchMapping(keys, values, rest):
            parts = [f"{expr(k)}: {pattern(v)}" for k, v in zip(keys, values)] + ([f"**{rest.id}"] if rest else [])
            return "{" + ", ".join(parts) + "}"
        case A.MatchClass(cls, args, names, kw):
            parts = [pattern(a) for a in args] + [f"{n}={pattern(v)}" for n, v in zip(names, kw)]
            return f"{expr(cls)}({', '.join(parts)})"
    raise TypeError(f"can't dump {p!r}")


def tparams(names: list[str]) -> str:
    return f"[{', '.join(names)}]" if names else ""


def param(p: A.Param) -> str:
    text = ("*" if p.star else "") + p.name
    if p.annotation:
        text += f": {p.annotation}"
    if p.default:
        text += f" = {expr(p.default)}"
    return text


def alias(a: A.Alias) -> str:
    return f"{a.name} as {a.asname}" if a.asname else a.name
