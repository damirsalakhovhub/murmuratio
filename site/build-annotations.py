"""Build inline handwriting: python3 site/build-annotations.py /path/to/Caveat.ttf.

Requires fonttools. Font source (OFL):
https://github.com/google/fonts/tree/main/ofl/caveat
The font is a build input only; the page ships paths and makes no font requests.
"""
from html import escape
from pathlib import Path
import re
import sys

from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

NOTE_LINES = [
    ["this tool gets the code today"],
    ["outer ring: limit used", "inner ring: time gone in the window"],
    ["your orchestrator"],
    ["hidden codex worker:", "its model and what it is doing now"],
    ["context filling up?", "start a fresh worker"],
    ["waiting for you"],
    ["done workers sink here"],
]
NOTES = [" ".join(lines) for lines in NOTE_LINES]


def build(font_path):
    font = TTFont(font_path)
    glyphs, cmap = font.getGlyphSet(), font.getBestCmap()
    scale = 26 / font["head"].unitsPerEm

    def width(text):
        return sum(glyphs[cmap[ord(c)]].width * scale for c in text)

    def drawing(lines):
        uses = []
        for row, line in enumerate(lines):
            x = 2
            for char in line:
                glyph = glyphs[cmap[ord(char)]]
                if char != " ":
                    uses.append(f'<use href="#ng{ord(char)}" x="{x:.1f}" y="{24 + row * 27}"/>')
                x += glyph.width * scale
        return "".join(uses), len(lines) * 27 + 8

    definitions = []
    for char in sorted(set("".join(NOTES)) - {" "}):
        pen = SVGPathPen(glyphs, ntos=lambda n: format(n, ".1f").rstrip("0").rstrip(".") if n else "0")
        glyphs[cmap[ord(char)]].draw(TransformPen(pen, (scale, 0, 0, -scale, 0, 0)))
        definitions.append(f'<path id="ng{ord(char)}" d="{pen.getCommands()}"/>')
    items = []
    for i, text in enumerate(NOTES, 1):
        lines = NOTE_LINES[i - 1]
        max_width = round(max(width(line) for line in lines) + 6)
        drawings = []
        for variant in ["margin", "list"]:
            paths, height = drawing(lines)
            drawings.append(f'<svg class="handwriting handwriting-{variant}" viewBox="0 0 {max_width} {height}" width="{max_width}" height="{height}" aria-hidden="true">{paths}</svg>')
        items.append(f'<li id="board-note-{i}" data-note="{i}" style="--note-width:{max_width}px" data-lines="{len(lines)}"><span class="note-number" aria-hidden="true">{i}</span><span class="annotation-text">{escape(text)}</span>{"".join(drawings)}</li>')
    block = '<!-- handwriting:start -->\n<svg width="0" height="0" aria-hidden="true" style="position:absolute"><defs>' + "".join(definitions) + '</defs></svg>\n<ol class="board-notes" aria-label="Board annotations">\n' + "\n".join(items) + '\n</ol>\n<!-- handwriting:end -->'
    page = Path(__file__).with_name("index.html")
    source = page.read_text()
    source, count = re.subn(r"<!-- handwriting:start -->[\s\S]*?<!-- handwriting:end -->", lambda _: block, source)
    if count != 1:
        raise SystemExit("Expected one handwriting block")
    page.write_text(source)


if __name__ == "__main__":
    build(sys.argv[1])
