"""Preserve default device color spaces discarded by redaction cleaning."""
import hashlib
import re

_DEFAULTS = ("DefaultRGB", "DefaultCMYK", "DefaultGray")


def _dictionary(doc, xref, key):
    kind, value = doc.xref_get_key(xref, key)
    if kind == "xref":
        return int(value.split()[0]), ""
    if kind == "dict":
        return xref, key + "/"
    return None


def _resources(doc, xref, inherited=False):
    seen = set()
    while xref not in seen:
        seen.add(xref)
        target = _dictionary(doc, xref, "Resources")
        if target or not inherited:
            return target
        kind, value = doc.xref_get_key(xref, "Parent")
        if kind != "xref":
            break
        xref = int(value.split()[0])
    return None


def _spaces(doc, resources):
    if not resources:
        return {}
    xref, prefix = resources
    colors = _dictionary(doc, xref, prefix + "ColorSpace")
    if not colors:
        return {}
    xref, prefix = colors
    return {name: value for name in _DEFAULTS
            for kind, value in [doc.xref_get_key(xref, prefix + name)]
            if kind != "null"}


def _images(doc, xref, seen=None):
    seen = set() if seen is None else seen
    if xref in seen:
        return []
    seen.add(xref)
    if doc.xref_get_key(xref, "Subtype")[1] == "/Image":
        return [hashlib.sha256(doc.xref_stream(xref)).hexdigest()]
    resources = _resources(doc, xref)
    if not resources:
        return []
    owner, prefix = resources
    kind, value = doc.xref_get_key(owner, prefix + "XObject")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]))
    result = []
    for ref in re.findall(r"(\d+)\s+\d+\s+R", value):
        result.extend(_images(doc, int(ref), seen))
    return result


def _signature(doc, xref):
    kind, value = doc.xref_get_key(xref, "BBox")
    bbox = tuple(float(n) for n in re.findall(r"[-+]?(?:\d*\.\d+|\d+)", value))
    return bbox, tuple(sorted(_images(doc, xref)))


def snapshot_default_spaces(page):
    """Capture page/Form defaults before cleaning, keeping same-document refs."""
    doc = page.parent
    forms = []
    for xref in sorted({item[0] for item in page.get_xobjects()}):
        spaces = _spaces(doc, _resources(doc, xref))
        if spaces:
            forms.append({"xref": xref, "signature": _signature(doc, xref), "spaces": spaces})
    return {"document_identity": id(doc), "page_xref": page.xref,
            "page_spaces": _spaces(doc, _resources(doc, page.xref, inherited=True)),
            "forms": forms}


def _restore(doc, xref, spaces):
    if not spaces:
        return 0
    resources = _resources(doc, xref)
    if not resources:
        # Materialize inherited resources without discarding their other entries.
        inherited = _resources(doc, xref, inherited=True)
        if inherited:
            owner, prefix = inherited
            value = doc.xref_object(owner) if not prefix else doc.xref_get_key(owner, prefix[:-1])[1]
            doc.xref_set_key(xref, "Resources", value)
        else:
            doc.xref_set_key(xref, "Resources", "<<>>")
        resources = _resources(doc, xref)
    owner, prefix = resources
    colors = _dictionary(doc, owner, prefix + "ColorSpace")
    if not colors:
        doc.xref_set_key(owner, prefix + "ColorSpace", "<<>>")
        colors = _dictionary(doc, owner, prefix + "ColorSpace")
    owner, prefix = colors
    changed = 0
    for name, value in spaces.items():
        if doc.xref_get_key(owner, prefix + name)[1] != value:
            doc.xref_set_key(owner, prefix + name, value)
            changed += 1
    return changed


def restore_default_spaces(page, snapshot):
    """Merge captured defaults into current page and matching cleaned Forms.

    Reloading a page is supported. Snapshots cannot be applied to a different
    document or page, since their references intentionally remain original.
    """
    doc = page.parent
    if id(doc) != snapshot["document_identity"] or page.xref != snapshot["page_xref"]:
        raise ValueError("Default color-space snapshot belongs to another document/page")
    by_signature = {}
    for item in snapshot["forms"]:
        signature = item["signature"]
        if signature in by_signature and by_signature[signature] != item["spaces"]:
            raise ValueError("Ambiguous Form default color spaces for identical geometry/images")
        by_signature[signature] = item["spaces"]
    changed = _restore(doc, page.xref, snapshot["page_spaces"])
    for xref in sorted({item[0] for item in page.get_xobjects()}):
        spaces = by_signature.get(_signature(doc, xref))
        if spaces:
            changed += _restore(doc, xref, spaces)
    return changed
