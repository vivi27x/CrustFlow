"""Colours SQL for the email and the sandbox.

Email clients strip stylesheets, so each colour is written inline on its token. Pygments escapes
everything it outputs, which is what makes the result safe to render as HTML.
"""

from __future__ import annotations

from markupsafe import Markup
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import PostgresLexer
from pygments.style import Style
from pygments.token import Comment, Keyword, Name, Number, String


class CodeCard(Style):
    """Picked for contrast on the card's navy. Anything unlisted stays the block's soft white."""

    styles = {
        Comment: "#8a8fa8",
        Keyword: "#a5b4fc",
        Name.Builtin: "#7dd3fc",
        Number: "#fcd34d",
        String: "#86efac",
    }


LEXER = PostgresLexer()
FORMATTER = HtmlFormatter(style=CodeCard, noclasses=True, nowrap=True)


def sql(code: str) -> Markup:
    return Markup(highlight(code, LEXER, FORMATTER).rstrip("\n"))
