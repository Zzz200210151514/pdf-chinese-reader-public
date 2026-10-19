"""Persistent, context-scoped terminology. User entries override shipped entries."""
from pathlib import Path
import hashlib
import json
import re
import yaml


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

    def validate(self, source, translation, domain='chemistry'):
        issues = []
        for e in self.inspect(source, domain):
            for word in e.get('forbidden', []):
                if word in translation:
                    issues.append({'source': e['source'], 'forbidden': word, 'expected': e['target'], 'domain': domain})
        return issues
