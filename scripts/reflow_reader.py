"""Deprecated HTML/Chromium reflow entry.

MinerU and local extraction differ only in ``prepare``. Both write
``source_map.json`` and continue through ``translator_v4.py`` plan, translate
and finish. This module remains so older imports keep working.
"""
from __future__ import annotations

import re
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.mineru_hybrid import normalize_items, stage_content_list
from core.pipeline import finish as pipeline_finish
from core.pipeline import prepare as pipeline_prepare

_TAGS = re.compile(r'</?(?:sub|sup|b|strong|i|em|br|scp)\b[^>]*>', re.I)


def compact(text):
    import html
    import unicodedata
    plain = html.unescape(_TAGS.sub('', text or ''))
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', plain)).replace('\u00ad', '')


def safe_markup(text):
    """Keep the small set of inline tags the old reader allowed."""
    import html
    from html.parser import HTMLParser

    class Markup(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_starttag(self, tag, attrs):
            if tag in {'sub', 'sup', 'b', 'strong', 'i', 'em', 'br'}:
                self.parts.append('<' + tag + '>')

        def handle_endtag(self, tag):
            if tag in {'sub', 'sup', 'b', 'strong', 'i', 'em'}:
                self.parts.append('</' + tag + '>')

        def handle_data(self, data):
            self.parts.append(html.escape(data))

    if not isinstance(text, str):
        raise ValueError('Text must be a string')
    parser = Markup()
    parser.feed(text)
    parser.close()
    return ''.join(parser.parts).replace('\n', ' ')


def media_at_readable_size(actual, expected):
    return all(actual[axis] + .75 >= expected[axis] * .99 for axis in ('width', 'height'))


def prepare(source, extract_json=None, work=None, **kwargs):
    """Stage a content list when one was already downloaded, then run in-place prepare."""
    warnings.warn('reflow_reader.prepare is deprecated. Use translator_v4.py prepare.',
                  DeprecationWarning, stacklevel=2)
    if work is None:
        raise TypeError('reflow_reader.prepare(source, extract_json, work) needs a work directory')
    if extract_json:
        stage_content_list(work, extract_json)
        return pipeline_prepare(source, work, extractor='mineru', mineru_cli=kwargs.get('mineru_cli'),
                                force=kwargs.get('force', False), reuse_extraction=True)
    return pipeline_prepare(source, work, config=kwargs.get('config'), extractor=kwargs.get('extractor', 'auto'),
                            mineru_cli=kwargs.get('mineru_cli'), force=kwargs.get('force', False))


def finish(*args, **kwargs):
    """Forward a translator_v4 finish call. The HTML reflow renderer is gone."""
    warnings.warn('reflow_reader.finish is deprecated. Use translator_v4.py finish.',
                  DeprecationWarning, stacklevel=2)
    if args and not isinstance(args[0], (str, Path)):
        raise RuntimeError('HTML/Chromium reflow finish has been removed. '
                           'Use translator_v4.py finish on the unified source map.')
    return pipeline_finish(*args, **kwargs)
