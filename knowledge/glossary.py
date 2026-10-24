"""Persistent, context-scoped terminology. User entries override shipped entries."""
from pathlib import Path
import hashlib
import json
import re
import yaml

# English phrases that legitimately introduce a normally forbidden Chinese word.
# `flow rate` may be translated 流量 next to flux/通量; `repulsion` may be 排斥
# next to rejection/截留率. An entry may override this with `counterparts`.
FORBIDDEN_COUNTERPARTS = {
    '流量': ('flow rate', 'flowrate', 'volumetric flow'),
    '排斥': ('repulsion', 'repulsive'),
}


class Glossary:
    def __init__(self, directory=None):
        self.directory = Path(directory or Path(__file__).parent / 'terminology')
        self.entries = []
        for path in sorted(self.directory.glob('*.yaml'), key=lambda p: (p.stem == 'user_dictionary', p.name)):
            data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
            for entry in data.get('terms', []):
                self.entries = [e for e in self.entries if (e['source'].lower(), e.get('domain', 'all')) != (entry['source'].lower(), entry.get('domain', 'all'))]
                self.entries.append(entry)
        self.version = hashlib.sha256(json.dumps(self.entries, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def inspect(self, source='', domain='chemistry'):
        return [dict(e) for e in self.entries if e.get('domain', 'all') in ('all', domain) and (not source or re.search(r'(?<!\w)' + re.escape(e['source']) + r'(?!\w)', source, re.I))]

    def apply(self, source, translation, domain='chemistry'):
        """Apply explicitly listed variants only; never replace arbitrary chemistry senses."""
        for e in self.inspect(source, domain):
            for variant in e.get('variants', []):
                translation = translation.replace(variant, e['target'])
        return translation

    def _counterpart_in_source(self, source, entry, word):
        listed = (entry.get('counterparts') or {}).get(word) or FORBIDDEN_COUNTERPARTS.get(word, ())
        for phrase in listed:
            if re.search(r'(?<!\w)' + re.escape(phrase) + r'(?!\w)', source, re.I):
                return True
        return False

    def validate(self, source, translation, domain='chemistry'):
        """Flag forbidden renderings without hard-blocking a paragraph that already complies.

        If the expected target is already in the translation, or the source also
        uses the English counterpart of the forbidden word, the co-occurrence is
        not an issue. Any remaining hit is a warning, not a hard failure.
        """
        issues = []
        for e in self.inspect(source, domain):
            target = e.get('target') or ''
            if target and target in translation:
                continue
            for word in e.get('forbidden', []):
                if word not in translation:
                    continue
                if self._counterpart_in_source(source, e, word):
                    continue
                issues.append({'source': e['source'], 'forbidden': word, 'expected': target,
                               'domain': domain, 'severity': 'warning'})
        return issues
