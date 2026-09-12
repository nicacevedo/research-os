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

#: The control characters a rendered report keeps, because they are its layout.
DISPLAY_KEPT: frozenset[str] = frozenset({"\n", "\t"})


def terminal_safe(text: str, *, keep: frozenset[str] = DISPLAY_KEPT) -> str:
    """Return ``text`` with every control character but ``keep`` made visible.

    Applied to already-rendered human output, so the newlines and tabs that are
    the report's own structure survive and everything else becomes a literal
    ``\\xNN``. A report that shows ``\\x1b[2J`` is telling the reader exactly
    what the untrusted string contained, which is the honest rendering; a report
    that cleared their screen would not be.
    """

    unsafe = CONTROL_CHARS - keep
    if not any(character in unsafe for character in text):
        return text
    return "".join(
        f"\\x{ord(character):02x}" if character in unsafe else character
        for character in text
    )
