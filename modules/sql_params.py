"""Replace string literals in predicates with bound parameters (``?``).

The text of the query is otherwise left byte-for-byte unchanged (token offsets, not a re-render),
so execution semantics cannot drift. Only literals that are comparison operands, LIKE patterns,
IN-list members or BETWEEN bounds are bound; select-list labels, LIMIT/OFFSET and anything we are not
sure about stay inline. Binding removes quoting/escaping from the picture entirely for filter values
(a value containing ' or ; can never alter the statement)."""
from __future__ import annotations

from typing import List, Tuple

from sqlglot.tokens import Tokenizer, TokenType

_CMP = {TokenType.EQ, TokenType.NEQ, TokenType.LT, TokenType.GT, TokenType.LTE, TokenType.GTE, TokenType.LIKE,
        TokenType.ILIKE, TokenType.GLOB, TokenType.BETWEEN}


def parameterize_literals(sql: str) -> Tuple[str, List[str]]:
    try:
        tokens = Tokenizer(dialect="sqlite").tokenize(sql)
    except Exception:
        return sql, []
    out, params, last = [], [], 0
    in_list_depth = 0          # >0 while inside IN ( ... ) with no nested SELECT
    paren_stack = []
    for i, t in enumerate(tokens):
        prev = tokens[i - 1] if i else None
        if t.token_type == TokenType.L_PAREN:
            is_in = prev is not None and prev.token_type == TokenType.IN
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            paren_stack.append(is_in and not (nxt is not None and nxt.token_type in (TokenType.SELECT, TokenType.WITH)))
        elif t.token_type == TokenType.R_PAREN and paren_stack:
            paren_stack.pop()
        if t.token_type != TokenType.STRING:
            continue
        bind = False
        if prev is not None and prev.token_type in _CMP:
            bind = True
        elif prev is not None and prev.token_type == TokenType.AND and any(
                x.token_type == TokenType.BETWEEN for x in tokens[max(0, i - 4):i]):
            bind = True
        elif paren_stack and paren_stack[-1] and prev is not None and prev.token_type in (TokenType.L_PAREN, TokenType.COMMA):
            bind = True
        elif prev is not None and prev.token_type == TokenType.NOT and i >= 2 and tokens[i - 2].token_type == TokenType.EQ:
            bind = False
        if bind:
            out.append(sql[last:t.start])
            out.append("?")
            params.append(t.text)
            last = t.end + 1
    out.append(sql[last:])
    return "".join(out), params
