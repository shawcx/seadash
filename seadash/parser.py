"""Parser: tokens -> syntax tree.

A hand-written recursive-descent parser. Statements get one method each.
Expressions follow Python's precedence levels, lowest to highest:

    x if c else y                     parse_expr
    or                                parse_or
    and                               parse_and
    not                               parse_not
    < > == != <= >= in not in is ...  parse_comparison   (chained: a < b < c)
    | ^ & << >> + - * / // % @        parse_binary       (precedence climbing)
    unary - + ~                       parse_unary
    **                                parse_power        (right-assoc, binds tighter than unary on its left)
    call, index, slice, .attr         parse_postfix
    literals, names, (...) [...] {...} parse_atom

The binary operators share one precedence-climbing loop driven by the
BINARY_PRECEDENCE table, rather than one method per level.
"""

from . import ast as A
from .errors import Loc, ParseError
from .lexer import FStringExpr, Token, TokenKind as K, tokenize

BINARY_PRECEDENCE = {
    "|": 1,
    "^": 2,
    "&": 3,
    "<<": 4, ">>": 4,
    "+": 5, "-": 5,
    "*": 6, "/": 6, "//": 6, "%": 6, "@": 6,
}

COMPARISON_OPS = {"<", ">", "==", "!=", "<=", ">="}

AUGMENTED_OPS = {"+=", "-=", "*=", "/=", "//=", "%=", "**=", "@=", "&=", "|=", "^=", "<<=", ">>="}

# Python keywords we recognise but don't implement yet, so users get an honest error.
NOT_YET_SUPPORTED = {"del", "yield"}


def parse(source: str) -> A.Module:
    return Parser(tokenize(source)).parse_module()


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.i = 0

    # ---- token helpers -----------------------------------------------------

    def peek(self, offset: int = 0) -> Token:
        return self.tokens[min(self.i + offset, len(self.tokens) - 1)]

    def next(self) -> Token:
        tok = self.tokens[self.i]
        if tok.kind != K.EOF:
            self.i += 1
        return tok

    def at(self, value: str, offset: int = 0) -> bool:
        """Is the token an operator or keyword spelled `value`?"""
        tok = self.peek(offset)
        return tok.kind in (K.OP, K.KEYWORD) and tok.value == value

    def at_kind(self, kind: K) -> bool:
        return self.peek().kind == kind

    def accept(self, value: str) -> Token | None:
        return self.next() if self.at(value) else None

    def expect(self, value: str, context: str = "") -> Token:
        if self.at(value):
            return self.next()
        raise self.error(f"expected '{value}'{context}, found {describe(self.peek())}")

    def expect_kind(self, kind: K, what: str) -> Token:
        if self.at_kind(kind):
            return self.next()
        raise self.error(f"expected {what}, found {describe(self.peek())}")

    def expect_name(self, what: str) -> Token:
        return self.expect_kind(K.NAME, what)

    def error(self, message: str, loc: Loc | None = None) -> ParseError:
        return ParseError(message, loc or self.peek().loc)

    # ---- statements --------------------------------------------------------

    def parse_module(self) -> A.Module:
        body: list[A.Stmt] = []
        while not self.at_kind(K.EOF):
            body += self.parse_statement()
        return A.Module(body, loc=Loc(1, 1))

    def parse_statement(self) -> list[A.Stmt]:
        tok = self.peek()
        if tok.kind == K.INDENT:
            raise self.error("unexpected indent")
        if tok.kind == K.KEYWORD:
            match tok.value:
                case "if":
                    return [self.parse_if()]
                case "while":
                    return [self.parse_while()]
                case "for":
                    return [self.parse_for()]
                case "def":
                    return [self.parse_def()]
                case "class" | "struct":
                    return [self.parse_class()]
                case "try":
                    return [self.parse_try()]
                case "with":
                    return [self.parse_with()]
                case "elif" | "else":
                    raise self.error(f"'{tok.value}' without a matching 'if'")
                case "except" | "finally":
                    raise self.error(f"'{tok.value}' without a matching 'try'")
                case kw if kw in NOT_YET_SUPPORTED:
                    raise self.error(f"'{kw}' is not supported yet")
        if self.at("@"):
            return [self.parse_decorated()]
        return self.parse_simple_statements()

    def parse_simple_statements(self) -> list[A.Stmt]:
        """One line of `;`-separated simple statements, through the NEWLINE."""
        stmts = [self.parse_simple_statement()]
        while self.accept(";"):
            if self.at_kind(K.NEWLINE):
                break
            stmts.append(self.parse_simple_statement())
        self.expect_kind(K.NEWLINE, "end of line")
        return stmts

    def parse_simple_statement(self) -> A.Stmt:
        tok = self.peek()
        loc = tok.loc
        if tok.kind == K.KEYWORD:
            match tok.value:
                case "pass":
                    self.next()
                    return A.Pass(loc=loc)
                case "break":
                    self.next()
                    return A.Break(loc=loc)
                case "continue":
                    self.next()
                    return A.Continue(loc=loc)
                case "return":
                    self.next()
                    value = None if self.at_statement_end() else self.parse_expr_list()
                    return A.Return(value, loc=loc)
                case "raise":
                    self.next()
                    if self.at_statement_end():
                        return A.Raise(loc=loc)
                    exc = self.parse_expr()
                    cause = self.parse_expr() if self.accept("from") else None
                    return A.Raise(exc, cause, loc=loc)
                case "nonlocal" | "global":
                    self.next()
                    names = [self.expect_name(f"a name after '{tok.value}'").value]
                    while self.accept(","):
                        names.append(self.expect_name(f"a name after '{tok.value}'").value)
                    return (A.Nonlocal if tok.value == "nonlocal" else A.Global)(names, loc=loc)
                case "assert":
                    self.next()
                    test = self.parse_expr()
                    msg = self.parse_expr() if self.accept(",") else None
                    return A.Assert(test, msg, loc=loc)
                case "import":
                    return self.parse_import()
                case "from":
                    return self.parse_from_import()
                case kw if kw in NOT_YET_SUPPORTED:
                    raise self.error(f"'{kw}' is not supported yet")

        first = self.parse_expr_list()

        if self.at(":"):
            self.next()
            self.check_single_target(first, "annotate")
            annotation = self.parse_type()
            value = self.parse_expr_list() if self.accept("=") else None
            return A.AnnAssign(first, annotation, value, loc=loc)

        if self.peek().kind == K.OP and self.peek().value in AUGMENTED_OPS:
            op = self.next().value[:-1]
            self.check_single_target(first, "assign to")
            return A.AugAssign(first, op, self.parse_expr_list(), loc=loc)

        if self.at("="):
            targets = [first]
            while self.accept("="):
                targets.append(self.parse_expr_list())
            value = targets.pop()
            for t in targets:
                self.check_target(t)
            return A.Assign(targets, value, loc=loc)

        return A.ExprStmt(first, loc=loc)

    def at_statement_end(self) -> bool:
        return self.at_kind(K.NEWLINE) or self.at(";")

    def check_target(self, e: A.Expr) -> None:
        """Can `e` appear on the left of `=`?"""
        match e:
            case A.Name() | A.Attribute() | A.Index():
                return
            case A.TupleLit(elts) | A.ListLit(elts):
                for elt in elts:
                    self.check_target(elt)
                return
        raise self.error(f"cannot assign to {describe_expr(e)}", e.loc)

    def check_single_target(self, e: A.Expr, verb: str) -> None:
        if not isinstance(e, (A.Name, A.Attribute, A.Index)):
            raise self.error(f"cannot {verb} {describe_expr(e)}", e.loc)

    def parse_block(self, owner: str) -> list[A.Stmt]:
        """`: NEWLINE INDENT stmts DEDENT`, or `: stmt` on the same line."""
        self.expect(":", f" after {owner}")
        if not self.at_kind(K.NEWLINE):
            return self.parse_simple_statements()
        self.next()
        if not self.at_kind(K.INDENT):
            raise self.error(f"expected an indented block after {owner}")
        self.next()
        body: list[A.Stmt] = []
        while not self.at_kind(K.DEDENT):
            body += self.parse_statement()
        self.next()
        return body

    def parse_if(self) -> A.If:
        loc = self.next().loc  # 'if' or 'elif'
        test = self.parse_named_expr()
        body = self.parse_block("'if' statement")
        orelse: list[A.Stmt] = []
        if self.at("elif"):
            orelse = [self.parse_if()]
        elif self.accept("else"):
            orelse = self.parse_block("'else'")
        return A.If(test, body, orelse, loc=loc)

    def parse_while(self) -> A.While:
        loc = self.next().loc
        test = self.parse_named_expr()
        body = self.parse_block("'while' statement")
        orelse = self.parse_block("'else'") if self.accept("else") else []
        return A.While(test, body, orelse, loc=loc)

    def parse_for(self) -> A.For:
        loc = self.next().loc
        target = self.parse_target_list()
        self.expect("in", " in 'for' statement")
        it = self.parse_expr_list()
        body = self.parse_block("'for' statement")
        orelse = self.parse_block("'else'") if self.accept("else") else []
        return A.For(target, it, body, orelse, loc=loc)

    def parse_decorated(self) -> A.FunctionDef | A.ClassDef:
        """@decorator lines (any expression, as in Python 3.9+) before a def or class."""
        decorators: list[A.Expr] = []
        while self.accept("@"):
            decorators.append(self.parse_named_expr())
            self.expect_kind(K.NEWLINE, "end of line after a decorator")
        if self.at("def"):
            node = self.parse_def()
        elif self.at("class") or self.at("struct"):
            node = self.parse_class()
        else:
            raise self.error(f"expected 'def' or 'class' after decorators, found {describe(self.peek())}")
        node.decorators = decorators
        return node

    def parse_try(self) -> A.Try:
        loc = self.next().loc
        body = self.parse_block("'try'")
        handlers: list[A.ExceptHandler] = []
        while tok := self.accept("except"):
            if handlers and handlers[-1].type is None:
                raise self.error("a bare 'except:' must be the last except clause", handlers[-1].loc)
            exc_type = None
            name = None
            if not self.at(":"):
                exc_type = self.parse_expr()
                if self.accept("as"):
                    name_tok = self.expect_name("a name after 'as'")
                    name = A.Name(name_tok.value, loc=name_tok.loc)
            handlers.append(A.ExceptHandler(exc_type, name, self.parse_block("'except'"), loc=tok.loc))
        orelse: list[A.Stmt] = []
        if tok := self.accept("else"):
            if not handlers:
                raise self.error("'else' after 'try' needs at least one 'except' clause", tok.loc)
            orelse = self.parse_block("'else'")
        finalbody = self.parse_block("'finally'") if self.accept("finally") else []
        if not handlers and not finalbody:
            raise self.error(f"expected 'except' or 'finally' after the 'try' block, found {describe(self.peek())}")
        return A.Try(body, handlers, orelse, finalbody, loc=loc)

    def parse_with(self) -> A.With:
        """`with a as x, b as y:`, also parenthesized: `with (a as x, b as y):`."""
        loc = self.next().loc
        parenthesized = self.at("(") and self.parenthesized_with_items()
        if parenthesized:
            self.next()
        items = [self.parse_with_item()]
        while self.accept(","):
            if parenthesized and self.at(")"):
                break
            items.append(self.parse_with_item())
        if parenthesized:
            self.expect(")", " after with items")
        return A.With(items, self.parse_block("'with'"), loc=loc)

    def parenthesized_with_items(self) -> bool:
        """Is `with (` the start of an item list, rather than a parenthesized expression?"""
        depth = 0
        for i in range(self.i, len(self.tokens)):
            tok = self.tokens[i]
            if tok.kind == K.OP and tok.value in "([{":
                depth += 1
            elif tok.kind == K.OP and tok.value in ")]}":
                depth -= 1
                if depth == 0:
                    return self.tokens[i + 1].kind == K.OP and self.tokens[i + 1].value == ":" and any(
                        t.kind == K.KEYWORD and t.value == "as" for t in self.tokens[self.i : i]
                    )
            elif tok.kind == K.NEWLINE:
                return False
        return False

    def parse_with_item(self) -> A.WithItem:
        context = self.parse_expr()
        target = None
        if self.accept("as"):
            # A single target: in `with a as x, b:` the comma starts the next item.
            target = self.parse_binary()
            self.check_target(target)
        return A.WithItem(context, target, loc=context.loc)

    def parse_def(self) -> A.FunctionDef:
        loc = self.next().loc
        name = self.expect_name("function name").value
        type_params = self.parse_type_params()
        self.expect("(", " after function name")
        params = self.parse_params()
        self.expect(")", " after parameters")
        returns = self.parse_type() if self.accept("->") else None
        body = self.parse_block(f"function definition '{name}'")
        return A.FunctionDef(name, params, returns, body, type_params, loc=loc)

    def parse_params(self) -> list[A.Param]:
        params: list[A.Param] = []
        seen: set[str] = set()
        while not self.at(")"):
            tok = self.expect_name("parameter name")
            if tok.value in seen:
                raise self.error(f"duplicate parameter '{tok.value}'", tok.loc)
            seen.add(tok.value)
            annotation = self.parse_type() if self.accept(":") else None
            default = self.parse_expr() if self.accept("=") else None
            if default is None and params and params[-1].default is not None:
                raise self.error("parameter without a default follows parameter with a default", tok.loc)
            params.append(A.Param(tok.value, annotation, default, loc=tok.loc))
            if not self.accept(","):
                break
        return params

    def parse_type_params(self) -> list[str]:
        """Generic parameters: the `[T, U]` in `def f[T, U](...)`."""
        if not self.accept("["):
            return []
        names = [self.expect_name("type parameter name").value]
        while self.accept(",") and not self.at("]"):
            names.append(self.expect_name("type parameter name").value)
        self.expect("]", " after type parameters")
        return names

    def parse_class(self) -> A.ClassDef:
        tok = self.next()
        name = self.expect_name(f"{tok.value} name").value
        type_params = self.parse_type_params()
        bases: list[A.TypeExpr] = []
        if self.accept("("):
            while not self.at(")"):
                bases.append(self.parse_type())
                if not self.accept(","):
                    break
            self.expect(")", " after base classes")
        body = self.parse_block(f"{tok.value} definition '{name}'")
        return A.ClassDef(tok.value, name, bases, body, type_params, loc=tok.loc)

    def parse_import(self) -> A.Import:
        loc = self.next().loc
        names = [self.parse_alias(dotted=True)]
        while self.accept(","):
            names.append(self.parse_alias(dotted=True))
        return A.Import(names, loc=loc)

    def parse_from_import(self) -> A.ImportFrom:
        loc = self.next().loc
        module = self.parse_dotted_name()
        self.expect("import", " in 'from' statement")
        parenthesized = self.accept("(")
        names = [self.parse_alias(dotted=False)]
        while self.accept(","):
            if parenthesized and self.at(")"):
                break
            names.append(self.parse_alias(dotted=False))
        if parenthesized:
            self.expect(")")
        return A.ImportFrom(module, names, loc=loc)

    def parse_alias(self, dotted: bool) -> A.Alias:
        loc = self.peek().loc
        name = self.parse_dotted_name() if dotted else self.expect_name("name to import").value
        asname = self.expect_name("name after 'as'").value if self.accept("as") else None
        return A.Alias(name, asname, loc=loc)

    def parse_dotted_name(self) -> str:
        parts = [self.expect_name("module name").value]
        while self.accept("."):
            parts.append(self.expect_name("name after '.'").value)
        return ".".join(parts)

    # ---- types -------------------------------------------------------------

    def parse_type(self) -> A.TypeExpr:
        """`T`, `T?`, `T | U`, `list[T]`, `mod.T`, `(T | U)?`"""
        first = self.parse_optional_type()
        if not self.at("|"):
            return first
        options = [first]
        while self.accept("|"):
            options.append(self.parse_optional_type())
        return A.UnionType(options, loc=first.loc)

    def parse_optional_type(self) -> A.TypeExpr:
        t = self.parse_type_atom()
        while tok := self.accept("?"):
            t = A.OptionalType(t, loc=tok.loc)
        return t

    def parse_type_atom(self) -> A.TypeExpr:
        tok = self.peek()
        if tok.kind == K.STRING:
            # A quoted annotation (Python's forward reference): -> "Pair[B, A]"
            self.next()
            sub = Parser(tokenize(f"({tok.value})", Loc(tok.loc.line, tok.loc.col)))
            sub.next()
            t = sub.parse_type()
            sub.expect(")", " after type")
            return t
        if self.accept("("):
            # `(T)` groups; `(A, B) -> R` and `() -> R` are function types.
            items: list[A.TypeExpr] = []
            while not self.at(")"):
                items.append(self.parse_type())
                if not self.accept(","):
                    break
            self.expect(")", " after type")
            if self.accept("->"):
                return A.FuncTypeExpr(items, self.parse_type(), loc=tok.loc)
            if len(items) != 1:
                raise self.error("expected '->' after a parameter list in a function type", self.peek().loc)
            return items[0]
        if self.accept("None"):
            return A.TypeName("None", loc=tok.loc)
        if tok.kind != K.NAME:
            raise self.error(f"expected a type, found {describe(tok)}")
        name = self.parse_dotted_name()
        args: list[A.TypeExpr] = []
        if name == "Callable" and self.accept("["):
            self.expect("[", " in Callable[[params], result]")
            params: list[A.TypeExpr] = []
            while not self.at("]"):
                params.append(self.parse_type())
                if not self.accept(","):
                    break
            self.expect("]", " after Callable parameter types")
            self.expect(",", " in Callable[[params], result]")
            ret = self.parse_type()
            self.expect("]", " after Callable result type")
            return A.FuncTypeExpr(params, ret, loc=tok.loc)
        if self.accept("["):
            args.append(self.parse_type())
            while self.accept(",") and not self.at("]"):
                args.append(self.parse_type())
            self.expect("]", " after type arguments")
        return A.TypeName(name, args, loc=tok.loc)

    # ---- expressions -------------------------------------------------------

    def parse_expr_list(self) -> A.Expr:
        """`a` or `a, b, c` (an unparenthesized tuple), as in `return a, b`."""
        first = self.parse_expr()
        if not self.at(","):
            return first
        elts = [first]
        while self.accept(","):
            if not starts_expression(self.peek()):
                break
            elts.append(self.parse_expr())
        return A.TupleLit(elts, loc=first.loc)

    def parse_target_list(self) -> A.Expr:
        """Loop targets: `x` or `i, x`. Parsed above comparisons so `in` isn't consumed."""
        first = self.parse_binary()
        target = first
        if self.at(","):
            elts = [first]
            while self.accept(","):
                if self.at("in"):
                    break
                elts.append(self.parse_binary())
            target = A.TupleLit(elts, loc=first.loc)
        self.check_target(target)
        return target

    def parse_named_expr(self) -> A.Expr:
        """An expression that may be a walrus: `n := len(xs)`."""
        if self.at_kind(K.NAME) and self.at(":=", 1):
            name_tok = self.next()
            op = self.next()
            value = self.parse_expr()
            return A.NamedExpr(A.Name(name_tok.value, loc=name_tok.loc), value, loc=op.loc)
        return self.parse_expr()

    def parse_expr(self) -> A.Expr:
        if self.at("lambda"):
            return self.parse_lambda()
        body = self.parse_or()
        if tok := self.accept("if"):
            test = self.parse_or()
            self.expect("else", " in conditional expression")
            orelse = self.parse_expr()
            return A.IfExp(test, body, orelse, loc=tok.loc)
        return body

    def parse_lambda(self) -> A.Lambda:
        loc = self.next().loc
        params: list[A.Param] = []
        while not self.at(":"):
            tok = self.expect_name("a lambda parameter name")
            if any(p.name == tok.value for p in params):
                raise self.error(f"duplicate parameter '{tok.value}'", tok.loc)
            if self.at("="):
                raise self.error("lambda parameters can't have default values", self.peek().loc)
            params.append(A.Param(tok.value, loc=tok.loc))
            if not self.accept(","):
                break
        self.expect(":", " after lambda parameters")
        return A.Lambda(params, self.parse_expr(), loc=loc)

    def parse_or(self) -> A.Expr:
        left = self.parse_and()
        while tok := self.accept("or"):
            left = A.BoolOp("or", left, self.parse_and(), loc=tok.loc)
        return left

    def parse_and(self) -> A.Expr:
        left = self.parse_not()
        while tok := self.accept("and"):
            left = A.BoolOp("and", left, self.parse_not(), loc=tok.loc)
        return left

    def parse_not(self) -> A.Expr:
        if tok := self.accept("not"):
            return A.UnaryOp("not", self.parse_not(), loc=tok.loc)
        return self.parse_comparison()

    def parse_comparison(self) -> A.Expr:
        left = self.parse_binary()
        ops: list[str] = []
        comparators: list[A.Expr] = []
        loc = None
        while op := self.accept_comparison_op():
            loc = loc or op[1]
            ops.append(op[0])
            comparators.append(self.parse_binary())
        if not ops:
            return left
        return A.Compare(left, ops, comparators, loc=loc)

    def accept_comparison_op(self) -> tuple[str, Loc] | None:
        tok = self.peek()
        if tok.kind == K.OP and tok.value in COMPARISON_OPS:
            self.next()
            return tok.value, tok.loc
        if self.at("in"):
            self.next()
            return "in", tok.loc
        if self.at("not") and self.at("in", 1):
            self.next()
            self.next()
            return "not in", tok.loc
        if self.at("is"):
            self.next()
            return ("is not" if self.accept("not") else "is"), tok.loc
        return None

    def parse_binary(self, min_prec: int = 1) -> A.Expr:
        left = self.parse_unary()
        while True:
            tok = self.peek()
            prec = BINARY_PRECEDENCE.get(tok.value) if tok.kind == K.OP else None
            if prec is None or prec < min_prec:
                return left
            self.next()
            right = self.parse_binary(prec + 1)  # +1: left-associative
            left = A.BinOp(tok.value, left, right, loc=tok.loc)

    def parse_unary(self) -> A.Expr:
        tok = self.peek()
        if tok.kind == K.OP and tok.value in ("-", "+", "~"):
            self.next()
            return A.UnaryOp(tok.value, self.parse_unary(), loc=tok.loc)
        return self.parse_power()

    def parse_power(self) -> A.Expr:
        base = self.parse_postfix()
        if tok := self.accept("**"):
            # The exponent may itself be unary (2 ** -1) and recurses back
            # here, which makes ** right-associative: 2 ** 3 ** 2 == 2 ** 9.
            return A.BinOp("**", base, self.parse_unary(), loc=tok.loc)
        return base

    def parse_postfix(self) -> A.Expr:
        e = self.parse_atom()
        while True:
            if self.at("("):
                e = self.parse_call(e)
            elif tok := self.accept("["):
                e = A.Index(e, self.parse_subscript(), loc=tok.loc)
                self.expect("]", " after subscript")
            elif self.accept("."):
                name = self.expect_name("attribute name after '.'")
                e = A.Attribute(e, name.value, loc=name.loc)
            else:
                return e

    def parse_call(self, func: A.Expr) -> A.Call:
        self.expect("(")
        args: list[A.Expr] = []
        keywords: list[A.Keyword] = []
        while not self.at(")"):
            if self.at_kind(K.NAME) and self.at("=", 1):
                name = self.next()
                self.next()
                if any(k.name == name.value for k in keywords):
                    raise self.error(f"keyword argument repeated: '{name.value}'", name.loc)
                keywords.append(A.Keyword(name.value, self.parse_expr(), loc=name.loc))
            else:
                if keywords:
                    raise self.error("positional argument follows keyword argument")
                arg = self.parse_named_expr()
                if self.at("for"):
                    # sum(x * x for x in xs): a generator as the only argument
                    arg = A.GeneratorExp(arg, self.parse_comprehension(), loc=arg.loc)
                    if args or not self.at(")"):
                        raise self.error("generator expression must be parenthesized", arg.loc)
                args.append(arg)
            if not self.accept(","):
                break
        self.expect(")", " to close function call")
        return A.Call(func, args, keywords, loc=func.loc)

    def parse_subscript(self) -> A.Expr:
        first = self.parse_subscript_item()
        if not self.at(","):
            return first
        items = [first]
        while self.accept(",") and not self.at("]"):
            items.append(self.parse_subscript_item())
        return A.TupleLit(items, loc=first.loc)

    def parse_subscript_item(self) -> A.Expr:
        """`i`, or a slice `lo:hi:step` with any part omitted."""
        loc = self.peek().loc
        lower = None if self.at(":") else self.parse_expr()
        if not self.accept(":"):
            return lower
        upper = None if self.at_any(":", "]", ",") else self.parse_expr()
        step = None
        if self.accept(":"):
            step = None if self.at_any("]", ",") else self.parse_expr()
        return A.Slice(lower, upper, step, loc=loc)

    def at_any(self, *values: str) -> bool:
        return any(self.at(v) for v in values)

    def parse_comprehension(self) -> list[A.Comprehension]:
        """One or more `for target in iter [if cond]...` clauses."""
        clauses = []
        while tok := self.accept("for"):
            target = self.parse_target_list()
            self.expect("in", " in comprehension")
            it = self.parse_or()
            ifs = []
            while self.accept("if"):
                ifs.append(self.parse_or())
            clauses.append(A.Comprehension(target, it, ifs, loc=tok.loc))
        return clauses

    # ---- atoms -------------------------------------------------------------

    def parse_atom(self) -> A.Expr:
        tok = self.peek()
        loc = tok.loc
        match tok.kind:
            case K.INT:
                self.next()
                return A.IntLit(tok.value, loc=loc)
            case K.FLOAT:
                self.next()
                return A.FloatLit(tok.value, loc=loc)
            case K.STRING | K.FSTRING:
                return self.parse_strings()
            case K.BYTES:
                return self.parse_bytes()
            case K.NAME:
                self.next()
                return A.Name(tok.value, loc=loc)
            case K.KEYWORD if tok.value in ("True", "False"):
                self.next()
                return A.BoolLit(tok.value == "True", loc=loc)
            case K.KEYWORD if tok.value == "None":
                self.next()
                return A.NoneLit(loc=loc)
            case K.KEYWORD if tok.value in NOT_YET_SUPPORTED:
                raise self.error(f"'{tok.value}' is not supported yet")
            case K.OP if tok.value == "(":
                return self.parse_paren()
            case K.OP if tok.value == "[":
                return self.parse_list()
            case K.OP if tok.value == "{":
                return self.parse_brace()
        raise self.error(f"expected an expression, found {describe(tok)}")

    def parse_paren(self) -> A.Expr:
        loc = self.next().loc
        if self.accept(")"):
            return A.TupleLit([], loc=loc)
        first = self.parse_named_expr()
        if self.at("for"):
            gen = A.GeneratorExp(first, self.parse_comprehension(), loc=loc)
            self.expect(")", " after generator expression")
            return gen
        if self.accept(")"):
            return first  # just grouping
        self.expect(",", " or ')' after expression")
        elts = [first]
        while not self.at(")"):
            elts.append(self.parse_named_expr())
            if not self.accept(","):
                break
        self.expect(")", " to close tuple")
        return A.TupleLit(elts, loc=loc)

    def parse_list(self) -> A.Expr:
        loc = self.next().loc
        if self.accept("]"):
            return A.ListLit([], loc=loc)
        first = self.parse_named_expr()
        if self.at("for"):
            comp = A.ListComp(first, self.parse_comprehension(), loc=loc)
            self.expect("]", " after list comprehension")
            return comp
        return A.ListLit(self.parse_rest_of_items(first, "]", self.parse_named_expr), loc=loc)

    def parse_brace(self) -> A.Expr:
        """`{}` is an empty dict (as in Python); otherwise a dict or set, maybe a comprehension."""
        loc = self.next().loc
        if self.accept("}"):
            return A.DictLit([], [], loc=loc)
        first = self.parse_expr()
        if self.accept(":"):
            value = self.parse_expr()
            if self.at("for"):
                comp = A.DictComp(first, value, self.parse_comprehension(), loc=loc)
                self.expect("}", " after dict comprehension")
                return comp
            keys, values = [first], [value]
            while self.accept(",") and not self.at("}"):
                keys.append(self.parse_expr())
                self.expect(":", " after dict key")
                values.append(self.parse_expr())
            self.expect("}", " to close dict")
            return A.DictLit(keys, values, loc=loc)
        if self.at("for"):
            comp = A.SetComp(first, self.parse_comprehension(), loc=loc)
            self.expect("}", " after set comprehension")
            return comp
        return A.SetLit(self.parse_rest_of_items(first, "}", self.parse_expr), loc=loc)

    def parse_rest_of_items(self, first: A.Expr, closer: str, parse_item) -> list[A.Expr]:
        items = [first]
        while self.accept(",") and not self.at(closer):
            items.append(parse_item())
        self.expect(closer, f" to close {'list' if closer == ']' else 'set'}")
        return items

    def parse_bytes(self) -> A.BytesLit:
        """Adjacent bytes literals concatenate: b"a" b"b" is b"ab"."""
        loc = self.peek().loc
        value = b""
        while self.at_kind(K.BYTES):
            value += self.next().value
        if self.at_kind(K.STRING) or self.at_kind(K.FSTRING):
            raise self.error("can't combine bytes and str literals")
        return A.BytesLit(value, loc=loc)

    def parse_strings(self) -> A.Expr:
        """Adjacent literals concatenate: "a" f"{b}" "c" is one string."""
        loc = self.peek().loc
        parts: list[str | A.FormattedValue] = []
        while self.at_kind(K.STRING) or self.at_kind(K.FSTRING):
            tok = self.next()
            if self.at_kind(K.BYTES):
                raise self.error("can't combine bytes and str literals")
            if tok.kind == K.STRING:
                add_text(parts, tok.value)
                continue
            for piece in tok.value:
                if isinstance(piece, str):
                    add_text(parts, piece)
                else:
                    if piece.debug is not None:  # {x=}: the source text, then the value (repr unless formatted)
                        add_text(parts, piece.debug)
                    parts.append(self.parse_fstring_expr(piece))
        if all(isinstance(p, str) for p in parts):
            return A.StrLit("".join(parts), loc=loc)
        return A.FString(parts, loc=loc)

    def parse_fstring_expr(self, piece: FStringExpr) -> A.FormattedValue:
        # Wrap in parens so the lexer ignores newlines and leading spaces inside
        # `{ ... }`; start one column early so locations still line up.
        start = Loc(piece.loc.line, piece.loc.col - 1)
        sub = Parser(tokenize(f"({piece.source})", start))
        value = sub.parse_atom()
        sub.expect_kind(K.NEWLINE, "'}' after f-string expression")
        conversion = piece.conversion
        if piece.debug is not None and conversion is None and piece.spec is None:
            conversion = "r"
        return A.FormattedValue(value, piece.spec, conversion, loc=piece.loc)


def add_text(parts: list, text: str) -> None:
    if parts and isinstance(parts[-1], str):
        parts[-1] += text
    elif text:
        parts.append(text)


def starts_expression(tok: Token) -> bool:
    match tok.kind:
        case K.NAME | K.INT | K.FLOAT | K.STRING | K.FSTRING | K.BYTES:
            return True
        case K.KEYWORD:
            return tok.value in ("True", "False", "None", "not", "lambda")
        case K.OP:
            return tok.value in ("(", "[", "{", "-", "+", "~")
    return False


def describe(tok: Token) -> str:
    match tok.kind:
        case K.NEWLINE:
            return "end of line"
        case K.INDENT:
            return "unexpected indent"
        case K.DEDENT:
            return "end of block"
        case K.EOF:
            return "end of file"
        case K.NAME:
            return f"name '{tok.value}'"
        case K.INT | K.FLOAT:
            return f"number {tok.value}"
        case K.STRING | K.FSTRING:
            return "string"
        case K.BYTES:
            return "bytes"
    return f"'{tok.value}'"


def describe_expr(e: A.Expr) -> str:
    match e:
        case A.IntLit() | A.FloatLit() | A.StrLit() | A.BytesLit() | A.BoolLit() | A.NoneLit() | A.FString():
            return "a literal"
        case A.Call():
            return "a function call"
        case A.ListLit() | A.DictLit() | A.SetLit():
            return "a literal"
        case A.TupleLit():
            return "a tuple"
        case A.Compare():
            return "a comparison"
    return "an expression"
