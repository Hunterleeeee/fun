"""A small, dependency-free syntax highlighter.

This is not a parser and does not try to be.  It is a single-pass regex scanner
that classifies a line of source into a handful of semantic token kinds, which
is all a terminal needs to make code readable.  Getting a nested template
literal slightly wrong is acceptable; corrupting the text is not, so the scanner
always emits every input character exactly once.

Adding a language means adding one :class:`Language` entry — no new code paths.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Token kinds map onto theme colour names in `components`.
KEYWORD = "keyword"
STRING = "string"
NUMBER = "number"
COMMENT = "comment"
NAME = "name"
CALL = "call"
OPERATOR = "operator"
TEXT = "text"


@dataclass(frozen=True)
class Language:
    names: tuple[str, ...]
    keywords: frozenset[str]
    line_comment: tuple[str, ...] = ()
    block_comment: tuple[tuple[str, str], ...] = ()
    strings: tuple[str, ...] = ("'", '"')
    triple_strings: tuple[str, ...] = ()
    builtins: frozenset[str] = field(default_factory=frozenset)


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


LANGUAGES: tuple[Language, ...] = (
    Language(
        ("python", "py"),
        _words("def class return if elif else for while import from as pass raise try except finally with lambda yield global nonlocal assert del in is not and or None True False await async match case"),
        line_comment=("#",),
        triple_strings=('"""', "'''"),
        builtins=_words("self cls print len range dict list set tuple str int float bool open isinstance super type enumerate zip map filter sorted any all"),
    ),
    Language(
        ("javascript", "js", "typescript", "ts", "jsx", "tsx"),
        _words("function return if else for while do switch case break continue const let var class extends new this super import export from default async await try catch finally throw typeof instanceof delete in of yield null undefined true false interface type enum implements public private protected readonly"),
        line_comment=("//",),
        block_comment=(("/*", "*/"),),
        strings=("'", '"', "`"),
        builtins=_words("console window document Promise Array Object String Number Boolean Math JSON require module exports"),
    ),
    Language(
        ("go",),
        _words("func return if else for range switch case break continue default package import type struct interface map chan go defer var const nil true false select fallthrough goto"),
        line_comment=("//",),
        block_comment=(("/*", "*/"),),
        strings=('"', "`"),
        builtins=_words("string int int64 int32 float64 bool byte rune error make new len cap append copy delete panic recover print println"),
    ),
    Language(
        ("rust", "rs"),
        _words("fn let mut const static struct enum impl trait use pub mod match if else for while loop return break continue where as dyn ref move unsafe async await crate self super true false Some None Ok Err"),
        line_comment=("//",),
        block_comment=(("/*", "*/"),),
        builtins=_words("String Vec Option Result Box Rc Arc HashMap i32 i64 u32 u64 usize f64 bool str println format vec"),
    ),
    Language(
        ("bash", "sh", "zsh", "shell", "console"),
        _words("if then else elif fi for while do done case esac function return export local readonly source alias unset in select until"),
        line_comment=("#",),
        builtins=_words("echo cd ls cat grep sed awk find git python python3 pip npm make curl test set trap exit"),
    ),
    Language(
        ("json",),
        _words("true false null"),
    ),
    Language(
        ("yaml", "yml"),
        _words("true false null yes no on off"),
        line_comment=("#",),
    ),
    Language(
        ("toml", "ini", "cfg"),
        _words("true false"),
        line_comment=("#", ";"),
    ),
    Language(
        ("sql",),
        _words("select from where insert update delete create table drop alter join left right inner outer on group by order having limit offset union all as and or not null distinct into values set index primary key foreign references"),
        line_comment=("--",),
        block_comment=(("/*", "*/"),),
    ),
)

_BY_NAME: dict[str, Language] = {name: language for language in LANGUAGES for name in language.names}

FALLBACK = Language((), frozenset(), line_comment=("#",))

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][-+]?\d+)?")
_OPERATOR = re.compile(r"[+\-*/%=<>!&|^~?:.,;(){}\[\]@]")


def resolve(language: str | None) -> Language:
    """Look up a language by name or common alias."""
    if not language:
        return FALLBACK
    return _BY_NAME.get(language.strip().lower().lstrip("."), FALLBACK)


def guess_language(path: str) -> str | None:
    """Infer a language name from a file path's extension."""
    if "." not in path:
        return None
    suffix = path.rsplit(".", 1)[-1].lower()
    return suffix if suffix in _BY_NAME else None


def tokenize(source: str, language: str | None = None) -> list[tuple[str, str]]:
    """Split ``source`` into ``(kind, text)`` spans covering every character."""
    spec = resolve(language)
    index = 0
    length = len(source)

    # Runs are accumulated as lists and joined once at the end.  Concatenating
    # into a tuple element defeats CPython's in-place string append, and single
    # characters are pushed one at a time (whitespace, and every non-ASCII prose
    # character, fall through to push(TEXT, char)) — so 400 KB of one kind took
    # four seconds, per repaint.
    runs: list[tuple[str, list[str]]] = []

    def push(kind: str, text: str) -> None:
        if not text:
            return
        if runs and runs[-1][0] == kind:
            runs[-1][1].append(text)
        else:
            runs.append((kind, [text]))

    while index < length:
        char = source[index]

        # Whitespace and newlines pass through untouched.
        if char in " \t\n\r":
            push(TEXT, char)
            index += 1
            continue

        comment_hit = next((marker for marker in spec.line_comment if source.startswith(marker, index)), None)
        if comment_hit:
            end = source.find("\n", index)
            end = length if end < 0 else end
            push(COMMENT, source[index:end])
            index = end
            continue

        block_hit = next((pair for pair in spec.block_comment if source.startswith(pair[0], index)), None)
        if block_hit:
            close = source.find(block_hit[1], index + len(block_hit[0]))
            end = length if close < 0 else close + len(block_hit[1])
            push(COMMENT, source[index:end])
            index = end
            continue

        triple_hit = next((marker for marker in spec.triple_strings if source.startswith(marker, index)), None)
        if triple_hit:
            close = source.find(triple_hit, index + len(triple_hit))
            end = length if close < 0 else close + len(triple_hit)
            push(STRING, source[index:end])
            index = end
            continue

        if char in spec.strings:
            cursor = index + 1
            while cursor < length:
                if source[cursor] == "\\":
                    cursor += 2
                    continue
                if source[cursor] == char:
                    cursor += 1
                    break
                if source[cursor] == "\n" and char != "`":
                    break  # unterminated single-line string
                cursor += 1
            push(STRING, source[index:cursor])
            index = cursor
            continue

        number = _NUMBER.match(source, index)
        if number and (index == 0 or not _IDENTIFIER.match(source[index - 1])):
            push(NUMBER, number.group())
            index = number.end()
            continue

        identifier = _IDENTIFIER.match(source, index)
        if identifier:
            word = identifier.group()
            end = identifier.end()
            if word in spec.keywords:
                kind = KEYWORD
            elif word in spec.builtins:
                kind = NAME
            elif end < length and source[end] == "(":
                kind = CALL
            else:
                kind = TEXT
            push(kind, word)
            index = end
            continue

        if _OPERATOR.match(char):
            push(OPERATOR, char)
            index += 1
            continue

        push(TEXT, char)
        index += 1

    return [(kind, "".join(parts)) for kind, parts in runs]


def tokenize_line(line: str, language: str | None = None) -> list[tuple[str, str]]:
    """Tokenize a single line, for renderers that work line by line."""
    return tokenize(line, language)
