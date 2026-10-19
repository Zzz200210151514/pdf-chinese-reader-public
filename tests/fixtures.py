"""Generate reproducible, explicitly synthetic PDF layout fixtures."""
from __future__ import annotations

from pathlib import Path
import pymupdf as fitz


def _page(doc: fitz.Document, title: str, subtitle: str, columns: int = 1) -> fitz.Page:
    page = doc.new_page(width=595, height=842)
    page.insert_text((42, 44), title, fontsize=15, fontname="helv")
    page.insert_text((42, 65), subtitle, fontsize=9, fontname="helv", color=(.25, .25, .25))
    page.draw_line((42, 75), (553, 75), color=(.2, .2, .2), width=.6)
    return page


def _body(page: fitz.Page, rect: fitz.Rect, prefix: str, count: int = 2, size: float = 8.5) -> None:
    narrow = rect.width < 350
    stride = 56 if narrow else 46
    box_height = 52 if narrow else 36
    for i in range(count):
        page.insert_textbox(fitz.Rect(rect.x0, rect.y0 + i * stride, rect.x1, rect.y0 + i * stride + box_height),
            f"{prefix} Paragraph {i + 1}. A reproducible synthetic scientific passage tests reading order, "
            "line wrapping, citations [1, 2], and stable text extraction. Measurements were 12.5 ± 0.3 units.",
            fontsize=size, fontname="helv", lineheight=1.4)


def _doc(path: Path, name: str, pages: int = 2, body: bool = True) -> fitz.Document:
    d = fitz.open()
    for n in range(pages):
        p = _page(d, f"Synthetic layout fixture: {name}", f"SIMULATED FORMAT — not a real journal paper · page {n + 1}")
        if body:
            _body(p, fitz.Rect(44, 98, 550, 160), name, 1)
            _body(p, fitz.Rect(44, 625, 550, 750), name, 2)
    d.set_toc([[1, "Synthetic fixture", 1], [2, "Details", 2]])
    return d


def _two_column(path: Path) -> None:
    d = fitz.open()
    p = _page(d, "Synthetic Elsevier-style two-column layout", "SIMULATED FORMAT — not an Elsevier paper")
    p.insert_text((42, 99), "ABSTRACT", fontsize=10, fontname="hebo")
    p.insert_textbox(fitz.Rect(42, 106, 553, 148), "Synthetic abstract spanning both columns. This is layout test content, not research.", fontsize=9)
    _body(p, fitz.Rect(42, 165, 292, 710), "Left column", 10, 8.5)
    _body(p, fitz.Rect(303, 165, 553, 710), "Right column", 10, 8.5)
    p.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(42, 738, 250, 752), "uri": "https://doi.org/10.5555/synthetic.2026.001"})
    p.insert_text((42, 749), "https://doi.org/10.5555/synthetic.2026.001", fontsize=5.7)
    d.new_page(); d[1].insert_text((42, 100), "References [1] Synthetic citation. https://example.org/synthetic", fontsize=9)
    d[1].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(42, 95, 250, 115), "page": 0, "to": fitz.Point(42, 95)})
    d.set_toc([[1, "Abstract", 1], [1, "References", 2]])
    d.save(path)


def _single_column(path: Path) -> None:
    d = _doc(path, "Nature-style single-column", 2)
    p = d[0]
    p.insert_text((42, 485), "Figure 1. Vector schematic: input → process → output", fontsize=8)
    p.draw_rect(fitz.Rect(70, 405, 190, 455), color=(.1, .35, .65), width=1.2)
    p.draw_rect(fitz.Rect(390, 405, 510, 455), color=(.65, .25, .1), width=1.2)
    p.draw_line((190, 430), (390, 430), color=(.2, .2, .2), width=1.5)
    p.draw_polyline([(390, 430), (379, 424), (379, 436), (390, 430)], color=(.2, .2, .2), fill=(.2, .2, .2))
    d.save(path)


def _table_heavy(path: Path) -> None:
    d = _doc(path, "ACS-style table-heavy", 3)
    p = d[0]
    p.insert_text((42, 185), "Table 1. Deterministic synthetic measurements", fontsize=9)
    rows = [["Sample", "Value", "Uncertainty", "Note"], ["A", "1.250", "0.030", "control"], ["B", "2.500", "0.040", "treated"], ["C", "3.750", "0.050", "repeat"]]
    x, y, widths, rh = 42, 200, [90, 100, 120, 160], 24
    for ri, row in enumerate(rows):
        xx = x
        for value, w in zip(row, widths):
            p.draw_rect(fitz.Rect(xx, y + ri*rh, xx+w, y+(ri+1)*rh), color=(.15,.15,.15), width=.5, fill=(.92,.94,.97) if ri == 0 else None)
            p.insert_text((xx+5, y+ri*rh+16), value, fontsize=8, fontname="hebo" if ri == 0 else "helv")
            xx += w
    p.insert_text((42, 310), "Data source: https://example.org/synthetic-table", fontsize=7)
    p.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(42, 300, 220, 316), "uri": "https://example.org/synthetic-table"})
    d.save(path)


def _formula_heavy(path: Path) -> None:
    d = _doc(path, "formula-heavy", 3)
    p = d[0]
    eqs = ["(1)  E = mc²", "(2)  f(x) = ∫₀∞ exp(−αt²) dt", "(3)  ∇·J + ∂ρ/∂t = 0", "(4)  σ² = Σᵢ (xᵢ − μ)² / N", "(5)  H₂O + CO₂ ⇌ H₂CO₃"]
    for i, eq in enumerate(eqs):
        p.insert_htmlbox(fitz.Rect(70, 180 + i*52, 525, 212 + i*52), f'<span style="font-family: sans-serif; font-size: 13pt">{eq}</span>')
        p.draw_line((62, 213+i*52), (548, 213+i*52), color=(.82,.82,.82), width=.35)
    d.save(path)


def _image_heavy(path: Path) -> None:
    d = _doc(path, "vector-figure-heavy", 2)
    p = d[0]
    colors = [(0.15,.45,.75),(.8,.3,.2),(.2,.65,.4),(.65,.4,.75)]
    for i, color in enumerate(colors):
        x, y = 58 + (i%2)*245, 220 + (i//2)*175
        tile = fitz.open(); tp = tile.new_page(width=360, height=210)
        tp.draw_rect(fitz.Rect(8,8,352,202), color=color, fill=(.96,.96,.96), width=3)
        for j in range(7):
            tp.draw_line((25,35+j*20),(330,35+j*20),color=color,width=2)
            tp.draw_circle((60+j*38, 35+j*20), 6, color=color, fill=color)
        pix = tp.get_pixmap(matrix=fitz.Matrix(.5,.5), alpha=False)
        png = pix.tobytes("png")
        p.insert_image(fitz.Rect(x,y,x+180,y+105), stream=png)
        p.insert_text((x,y+120), f"Figure {i+1}. Raster panel {chr(65+i)}.", fontsize=8)
    d.save(path)


def _reference_heavy(path: Path) -> None:
    d = _doc(path, "reference-heavy", 3, body=False)
    p = d[1]
    p.insert_text((42, 105), "References", fontsize=12, fontname="hebo")
    p.insert_text((42, 125), "Synthetic records — not published works", fontsize=8)
    for i in range(1, 24):
        y = 150 + (i-1)*28
        p.insert_textbox(fitz.Rect(45,y,550,y+23), f"[{i}] Example Author et al. Synthetic reference title {i}. DOI: 10.5555/example.{i:03d}  https://example.org/ref/{i}", fontsize=6.8)
        p.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(420,y,550,y+14), "uri": f"https://doi.org/10.5555/example.{i:03d}"})
    d.save(path)


def make_corpus(output_dir: str | Path) -> list[dict[str, str]]:
    """Create synthetic, offline fixtures. Existing files are replaced deterministically."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    specs = [
        ("elsevier_two_column.pdf", "double_column", _two_column),
        ("nature_single_column.pdf", "single_column", _single_column),
        ("acs_table_heavy.pdf", "table_heavy", _table_heavy),
        ("equation_dense.pdf", "formula_heavy", _formula_heavy),
        ("vector_figure_heavy.pdf", "image_heavy", _image_heavy),
        ("reference_dense.pdf", "reference_heavy", _reference_heavy),
    ]
    results = []
    for name, category, builder in specs:
        path = root / name
        builder(path)
        results.append({"name": name, "path": str(path), "category": category, "source_kind": "synthetic"})
    return results
