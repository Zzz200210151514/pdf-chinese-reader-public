"""Shared font defaults; caller overrides remain authoritative."""
from pathlib import Path
import warnings

DEFAULT_FONTS = {'regular': 'msyh.ttc', 'bold': 'msyhbd.ttc', 'fallback': 'seguisym.ttf'}


def _resolve_single_path(name, font_dir):
    """Return an existing font path for a name/absolute path, or None."""
    if not name:
        return None
    p = Path(name)
    if p.is_file():
        return p
    if font_dir is not None:
        candidate = Path(font_dir) / name
        if candidate.is_file():
            return candidate
    return None


def resolve_fonts(fonts=None, font_dir=None):
    """Resolve font family roles with graceful fallbacks.

    - fonts given: it is the base; only missing regular/bold defaults are filled.
      A legacy ``symbol`` entry acts as the ``fallback`` alias.
    - regular: required to exist when font_dir is given (ValueError otherwise).
    - bold: falls back to regular when its file is missing.
    - fallback: optional; a missing/None file degrades to a warning, not a failure.
    - names may be absolute paths, not just files inside font_dir.
    """
    if fonts is not None and not isinstance(fonts, dict):
        raise TypeError(f"fonts configuration must be a mapping, got {type(fonts).__name__}")

    if fonts:
        result = dict(fonts)
        result.setdefault('regular', DEFAULT_FONTS['regular'])
        result.setdefault('bold', result['regular'])
        if 'symbol' in result and 'fallback' not in result:
            result['fallback'] = result['symbol']
    else:
        result = dict(DEFAULT_FONTS)

    for role in ('regular', 'bold', 'fallback'):
        if role in result:
            value = result[role]
            if value is not None and not isinstance(value, (str, Path)):
                raise TypeError(f"Font role '{role}' must be a string path or null, got {type(value).__name__}")

    if font_dir is not None:
        directory = Path(font_dir)
        if not _resolve_single_path(result.get('regular'), directory):
            raise ValueError(f"Missing font (regular): {directory / str(result.get('regular'))}")
        if not _resolve_single_path(result.get('bold'), directory):
            # Graceful downgrade: bold reuses the regular face.
            result['bold'] = result['regular']
        fallback = result.get('fallback')
        if fallback and not _resolve_single_path(fallback, directory):
            warnings.warn(f"Fallback/symbol font missing ({fallback}); "
                          f"Greek/superscript glyphs will fall back to the regular font.", UserWarning)
            result['fallback'] = None
    return result
