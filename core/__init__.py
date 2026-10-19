"""Portable scientific PDF parsing, layout, rebuild and validation layers."""

ENGINE_REVISION = '4.1.9-relaxed-delivery'

def engine_identity():
    from pathlib import Path
    return {'revision':ENGINE_REVISION,'root':str(Path(__file__).resolve().parents[1])}
