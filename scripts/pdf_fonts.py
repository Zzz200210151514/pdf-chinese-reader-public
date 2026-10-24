"""Check actual font coverage and explicitly wrap fallback glyphs in PDF HTML."""
import html
import re
from html.parser import HTMLParser
from pathlib import Path
import pymupdf as fitz
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.fonts import resolve_fonts


def _load_font(name, fontdir):
    if not name:
        return None
    p = Path(name)
    if p.is_file():
        return fitz.Font(fontfile=str(p))
    if fontdir is not None:
        cand = Path(fontdir) / name
        if cand.is_file():
            return fitz.Font(fontfile=str(cand))
    return None


class FontCoverage:
    def __init__(self, manifest, fontdir):
        paths = resolve_fonts(manifest.get('fonts'), fontdir)
        self.regular = _load_font(paths.get('regular'), fontdir)
        if not self.regular:
            raise ValueError(f"Cannot load regular font: {paths.get('regular')}")
        self.bold = _load_font(paths.get('bold'), fontdir) or self.regular
        fb_name = paths.get('fallback') or paths.get('symbol')
        self.fallback = _load_font(fb_name, fontdir)
        self.cache = {}

    def has(self, ch, bold=False):
        key = (ch, bold)
        if key not in self.cache:
            font = self.bold if bold else self.regular
            self.cache[key] = bool(font.has_glyph(ord(ch)))
        return self.cache[key]

    def render(self, source, bold=False):
        parser = GlyphHTML(self, bold)
        parser.feed(source)
        parser.close()
        return ''.join(parser.parts), sorted(parser.missing)


class GlyphHTML(HTMLParser):
    def __init__(self, fonts, bold):
        super().__init__(convert_charrefs=True)
        self.fonts, self.bold = fonts, bold
        self.parts, self.missing = [], set()
        self.bold_depth = 0

    def handle_starttag(self, tag, attrs):
        self.parts.append(self.get_starttag_text())
        if tag in ('b', 'strong'):
            self.bold_depth += 1

    def handle_startendtag(self, tag, attrs):
        self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        self.parts.append(f'</{tag}>')
        if tag in ('b', 'strong'):
            self.bold_depth = max(0, self.bold_depth - 1)

    def handle_data(self, data):
        for ch in data:
            cp = ord(ch)
            if 0x80 <= cp <= 0x9F or 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0x10FFFD:
                continue
            escaped = html.escape(ch)
            if ch.isspace() or self.fonts.has(ch, self.bold or self.bold_depth > 0):
                self.parts.append(escaped)
            elif self.fonts.fallback and self.fonts.fallback.has_glyph(ord(ch)):
                self.parts.append('<span style="font-family:PdfFallback">' + escaped + '</span>')
            else:
                self.missing.add(ch)
                self.parts.append(escaped)


def scientific_symbols(text):
    return ''.join(ch for ch in text if 'Ͱ' <= ch <= 'Ͽ'
                   or '⁰' <= ch <= '₟'
                   or ch in '¹²³±×÷−≈≤≥∂∑∫∞')


def canonicalize_digit_cmaps(doc, first_xref):
    """Use ASCII for known digit aliases in newly embedded font ToUnicode maps.

    MuPDF can choose Noto's oldstyle-digit PUA aliases while embedding the
    glyph requested by an ASCII digit. Fix the mapping, not the rendered glyph
    or extracted prose; native source fonts are deliberately excluded.
    """
    seen = set()
    for xref in range(first_xref, doc.xref_length()):
        kind, value = doc.xref_get_key(xref, 'ToUnicode')
        if kind != 'xref':
            continue
        cmap = int(value.split()[0])
        if cmap in seen:
            continue
        seen.add(cmap)
        stream = doc.xref_stream(cmap).decode('ascii')
        def section(match):
            mode = match.group(1)
            def row(line):
                tokens = list(re.finditer(r'<([0-9a-fA-F]+)>', line))
                skip = 1 if mode == 'char' else 2
                for token in reversed(tokens[skip:]):
                    value = token.group(1)
                    if len(value) == 4 and 0xF6B1 <= int(value, 16) <= 0xF6BA:
                        digit = int(value, 16) - 0xF6B1
                        line = line[:token.start(1)] + f'{0x30 + digit:04x}' + line[token.end(1):]
                return line
            return 'beginbf' + mode + ''.join(row(line) for line in match.group(2).splitlines(True)) + 'endbf' + mode
        updated = re.sub(r'beginbf(char|range)(.*?)endbf\1', section, stream, flags=re.S)
        # Noto's proportional digit CIDs have no ToUnicode entry at all;
        # extraction falls back to the CID (U+F6B1 ... U+F6BA).
        _, basefont = doc.xref_get_key(xref, 'BaseFont')
        _, encoding = doc.xref_get_key(xref, 'Encoding')
        if 'Noto' in basefont and encoding == '/Identity-H':
            aliases = '\n10 beginbfchar\n' + ''.join(
                f'<{0xF6B1 + digit:04x}> <{0x30 + digit:04x}>\n' for digit in range(10)) + 'endbfchar\n'
            updated = updated.replace('endcmap', aliases + 'endcmap')
        if updated != stream:
            doc.update_stream(cmap, updated.encode('ascii'))
