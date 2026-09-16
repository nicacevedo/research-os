"""Making untrusted text safe to print at a human's terminal.

A run report quotes strings Research OS did not write: a planner's task title, an
analyst's finding, a reviewer's summary, the output a test suite printed. A
terminal reads some byte sequences as commands rather than as text -- ESC starts
an ANSI control sequence that can move the cursor, recolour the screen, retitle
the window, or overwrite a line that was already printed -- so a finding
containing one could make the report say something other than what is archived.

This module is the display boundary and nothing more:

* It is **not** the model-to-model boundary. Text going into another model's
  prompt passes through :mod:`research_os.automation.promptdata`, which is
  stricter: it also neutralises data-block delimiters and proves the fence it
  produced is unique. Nothing here replaces that.
* It **never** changes what is stored. The run archive keeps the raw bytes a
  provider returned, and their digests. This is applied to the rendered view a
  person reads, on its way to the terminal.

The transformation is deliberately dull. Every control character that is not
newline or tab is rewritten as its own visible escape -- ESC becomes the four
characters ``\\x1b`` -- so nothing is hidden from the reader and nothing is
interpreted by the terminal. Rewriting the characters rather than parsing escape
*sequences* means there is no sequence grammar to get wrong: a terminal cannot
act on an ESC it never receives, whatever follows it.

The same treatment is applied to :data:`DECEPTIVE_CHARS`, which are not control
characters and which a terminal handles perfectly correctly -- that is the
problem. They change what the reader sees while leaving the bytes intact, and
the reader is the person the authority boundary is protecting. See that
constant for what is in the set and, more importantly, what is not.
"""

from __future__ import annotations

#: Every character that must not reach a terminal, or a prompt, as itself.
#:
#: C0 (0x00-0x1F), DEL (0x7F), and C1 (0x80-0x9F). C1 is included because a
#: terminal in an 8-bit mode reads 0x9B as CSI, the same introducer ``ESC [``
#: produces, so filtering ESC alone would leave an equivalent path open.
CONTROL_CHARS: frozenset[str] = frozenset(
    chr(code) for code in (*range(0x20), 0x7F, *range(0x80, 0xA0))
)

#: Characters that are not control characters but are not text either.
#:
#: An adversarial review of the display boundary made the point that
#: :data:`CONTROL_CHARS` is the *terminal's* threat model, not the reader's. A
#: finding containing ``\u202e`` contains no control character at all; it
#: reverses the display order of everything after it, so the sentence a
#: researcher reads in the report is not the sentence the archive stores. The
#: same review showed a zero-width joiner splitting an experiment id so that two
#: different ids rendered identically.
#:
#: Four groups, and each one changes what a human sees without changing the
#: bytes:
#:
#: * bidirectional overrides and isolates -- ``\u061c``, ``\u200e``,
#:   ``\u200f``, ``\u202a``-``\u202e``, ``\u2066``-``\u2069``;
#: * zero-width and invisible characters -- ``\u00ad`` (soft hyphen),
#:   ``\u200b``-``\u200d``, ``\u2060``, ``\ufeff``;
#: * the Unicode line and paragraph separators ``\u2028`` and ``\u2029``,
#:   which some terminals and every text widget treat as line breaks although
#:   ``str.splitlines`` is the only thing in Python that agrees;
#: * interlinear annotation ``\ufff9``-``\ufffb`` and the deprecated tag
#:   characters ``\U000e0000``-``\U000e007f``, both of which carry text that
#:   renders as nothing.
#:
#: Deliberately *not* included, and this is the important half: **letters**.
#: Combining marks, emoji modifiers, confusables, and every strong
#: right-to-left *letter* pass through unchanged. A reviewer quoting a Hebrew
#: title or a finding naming an accented file must stay readable.
#:
#: The consequence is stated plainly because an earlier version of this comment
#: overstated what escaping buys. UAX#9 reorders a neutral run around any
#: character of `Bidi_Class` R or AL -- no override required -- so
#: ``holdout <hebrew> 0.42 0.91 baseline`` displays with the two numbers
#: swapped, with nothing escaped and the archived bytes unchanged. Escaping the
#: explicit *controls* is what this set does; it does not and cannot make a
#: rendered line equal its archived bytes in general. A renderer that wants that
#: guarantee has to wrap each untrusted field in ``U+2066``/``U+2069``, whose
#: forgery is prevented by their being in this set.
DECEPTIVE_CHARS: frozenset[str] = frozenset(
    (
        "\u061c",
        "\u200e",
        "\u200f",
        "\u00ad",
        "\u2060",
        "\ufeff",
        "\u2028",
        "\u2029",
        *(chr(code) for code in range(0x202A, 0x202F)),
        *(chr(code) for code in range(0x200B, 0x200E)),
        *(chr(code) for code in range(0xFFF9, 0xFFFC)),
        # Invisible operators, deprecated format characters, and the isolate
        # controls. `range(0x2061, 0x2070)` covers `U+2061`-`U+2064` (invisible
        # times, function application, separator, plus), `U+2065` (unassigned),
        # `U+2066`-`U+2069` (the isolates) and `U+206A`-`U+206F` (deprecated).
        # A second adversarial review built `exp-00042` with `U+2064` inside it
        # -- the module's own stated scenario, two ids rendering identically --
        # and `U+200D` was escaped while `U+2064` was not.
        *(chr(code) for code in range(0x2061, 0x2070)),
        # Mongolian free variation selectors and the vowel separator.
        *(chr(code) for code in range(0x180B, 0x1810)),
        # Zero-width glyphs that are not classed as format characters:
        # COMBINING GRAPHEME JOINER, the Hangul fillers, Khmer vowel inherent
        # signs. Each renders as nothing and each is accepted in an identifier.
        chr(0x034F),
        chr(0x115F),
        chr(0x1160),
        chr(0x3164),
        chr(0xFFA0),
        chr(0x17B4),
        chr(0x17B5),
        # Variation selectors. `range(0xE0000, 0xE0080)` escaped the tag block
        # and stopped immediately before `U+E0100`-`U+E01EF`, the variation
        # selector supplement in the same plane -- which is the current channel
        # for smuggling arbitrary ASCII into a single rendered glyph. The whole
        # plane goes, along with the BMP selectors at `U+FE00`-`U+FE0F`.
        *(chr(code) for code in range(0xFE00, 0xFE10)),
        *(chr(code) for code in range(0xE0000, 0xE1000)),
        # Musical and Duployan format controls, both zero-width.
        *(chr(code) for code in range(0x1BCA0, 0x1BCA4)),
        *(chr(code) for code in range(0x1D173, 0x1D17B)),
    )
)

#: The control characters a rendered report keeps, because they are its layout.
DISPLAY_KEPT: frozenset[str] = frozenset({"\n", "\t"})


def _escape(character: str) -> str:
    """One unsafe character as the visible text of its own code point."""

    code = ord(character)
    if code <= 0xFF:
        return f"\\x{code:02x}"
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def terminal_safe(text: str, *, keep: frozenset[str] = DISPLAY_KEPT) -> str:
    """Return ``text`` with every control character but ``keep`` made visible.

    Applied to already-rendered human output, so the newlines and tabs that are
    the report's own structure survive and everything else becomes a literal
    ``\\xNN``. A report that shows ``\\x1b[2J`` is telling the reader exactly
    what the untrusted string contained, which is the honest rendering; a report
    that cleared their screen would not be.
    """

    unsafe = (CONTROL_CHARS | DECEPTIVE_CHARS) - keep
    if not any(character in unsafe for character in text):
        return text
    return "".join(
        _escape(character) if character in unsafe else character for character in text
    )
