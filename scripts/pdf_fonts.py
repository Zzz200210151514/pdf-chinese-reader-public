"""Check actual font coverage and explicitly wrap fallback glyphs in PDF HTML."""
import html
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
