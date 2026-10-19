"""Per-validation immutable page display lists, never persisted across PDF edits."""
import pymupdf as fitz


def protected_pixmap(document, page_index, rectangle, display_lists):
    """Reuse a display list while preserving exact RGB 120 dpi clipped samples."""
    if page_index not in display_lists:
        display_lists[page_index] = document[page_index].get_displaylist(annots=True)
    return display_lists[page_index].get_pixmap(matrix=fitz.Matrix(120/72, 120/72),
        colorspace=fitz.csRGB, alpha=False, clip=rectangle)
