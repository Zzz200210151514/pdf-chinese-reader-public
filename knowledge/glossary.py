"""Persistent, context-scoped terminology. User entries override shipped entries."""
import csv
import hashlib
import json
import re
from pathlib import Path
import yaml

# English phrases that legitimately introduce a normally forbidden Chinese word.
# `flow rate` may be translated 流量 next to flux/通量; `repulsion` may be 排斥
# next to rejection/截留率. An entry may override this with `counterparts`.
FORBIDDEN_COUNTERPARTS = {
    '流量': ('flow rate', 'flowrate', 'volumetric flow'),
    '排斥': ('repulsion', 'repulsive'),
}


class Glossary:
    def __init__(self, directory=None, glossary_files=None):
        self.directory = Path(directory or Path(__file__).parent / 'terminology')
        self.entries = []
        for path in sorted(self.directory.glob('*.yaml'), key=lambda p: (p.stem == 'user_dictionary', p.name)):
            self._merge_entries(self._load_yaml_entries(path), is_override=(path.stem == 'user_dictionary'))
        for raw_path in self._normalize_files(glossary_files):
            path = self._resolve_file(raw_path)
            if not path.exists():
                raise FileNotFoundError(f'Glossary file not found: {path}')
            if path.suffix.lower() == '.csv':
                loaded = self._load_csv_entries(path)
            else:
                loaded = self._load_yaml_entries(path)
            self._merge_entries(loaded, is_override=True)
        for entry in self.entries:
            src = entry['source']
            entry['_lower'] = src.lower()
            entry['_pattern'] = re.compile(r'(?<!\w)' + re.escape(src) + r'(?!\w)', re.I)
        serializable = [{k: v for k, v in e.items() if not k.startswith('_')} for e in self.entries]
        self.version = hashlib.sha256(json.dumps(serializable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _normalize_files(glossary_files):
        if not glossary_files:
            return []
        if isinstance(glossary_files, (str, Path)):
            return [glossary_files]
        return list(glossary_files)

    @staticmethod
    def _resolve_file(raw_path):
        path = Path(raw_path)
        if path.is_absolute() or path.exists():
            return path
        skill_rel = Path(__file__).resolve().parent.parent / path
        return skill_rel if skill_rel.exists() else path

    @staticmethod
    def _load_yaml_entries(path):
        data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        return [dict(e) for e in data.get('terms', []) if isinstance(e, dict) and e.get('source')]

    @staticmethod
    def _load_csv_entries(path):
        entries = []
        section = ''
        with path.open('r', encoding='utf-8-sig', newline='') as handle:
            for raw in handle:
                stripped = raw.strip()
                if not stripped or stripped.lower() == 'source,target':
                    continue
                if stripped.startswith('#'):
                    section = stripped.lstrip('# ').strip()
                    continue
                row = next(csv.reader([stripped]))
                if len(row) >= 2 and row[0].strip() and row[1].strip():
                    entry = {'source': row[0].strip(), 'target': row[1].strip(), 'domain': 'all'}
                    if section:
                        entry['context'] = section
                    entries.append(entry)
        return entries

    def _merge_entries(self, loaded, *, is_override=False):
        for entry in loaded:
            src_low = entry['source'].lower()
            dom = entry.get('domain', 'all')
            if is_override and dom == 'all':
                self.entries = [e for e in self.entries if e['source'].lower() != src_low]
            else:
                self.entries = [e for e in self.entries if (e['source'].lower(), e.get('domain', 'all')) != (src_low, dom)]
            self.entries.append(dict(entry))

    def inspect(self, source='', domain='chemistry'):
        source_lower = source.lower() if source else ''
        matched = []
        for e in self.entries:
            if e.get('domain', 'all') not in ('all', domain):
                continue
            if not source or (e['_lower'] in source_lower and e['_pattern'].search(source)):
                matched.append({k: v for k, v in e.items() if not k.startswith('_')})
        return matched

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
