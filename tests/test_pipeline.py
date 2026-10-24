"""End-to-end prepare/review/mock-translate/finish checks on synthetic PDFs."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import traceback
import unittest

import pymupdf


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT.parent.parent
FIXTURES = WORK / "v4-upgrade" / "generated-fixtures"
REPORT = Path(os.environ.get("V4_PIPELINE_REPORT", WORK / "v4-upgrade" / "pipeline-fixtures-report.json"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from core.pipeline import finish, prepare, write
from fixtures import make_corpus
from llm import freeze


NAMES = [
    "elsevier_two_column.pdf",
    "nature_single_column.pdf",
    "acs_table_heavy.pdf",
    "equation_dense.pdf",
    "vector_figure_heavy.pdf",
    "reference_dense.pdf",
]
MOCK_TOKEN = re.compile(r"⟦V4_[a-f0-9]{8}_\d{4}⟧")
ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def mock_translation(source_text: str) -> str:
    """Translate each prose run to one Chinese glyph, retaining science tokens."""
    frozen_text, _protected = freeze(source_text)
    output = []
    cursor = 0
    for match in MOCK_TOKEN.finditer(frozen_text):
        segment = frozen_text[cursor:match.start()]
        if ASCII_WORD.search(segment):
            output.append("译" + ASCII_WORD.sub("", segment))
        else:
            output.append(segment)
        output.append(match.group(0))
        cursor = match.end()
    segment = frozen_text[cursor:]
    output.append("译" + ASCII_WORD.sub("", segment) if ASCII_WORD.search(segment) else segment)
    return "".join(output)


class PipelineFixtureTests(unittest.TestCase):
    def test_six_synthetic_fixtures_end_to_end(self):
        started = utc_now()
        prior_runs = []
        if REPORT.is_file():
            try:
                old = json.loads(REPORT.read_text(encoding="utf-8"))
                candidates = list(old.get("runs", []))
                if not candidates:
                    candidates = list(old.get("previous_runs", []))
                    candidates.append(old)
                seen = set()
                for item in candidates:
                    key = (item.get("started_utc"), item.get("ended_utc"))
                    if key not in seen:
                        prior_runs.append(item)
                        seen.add(key)
            except (OSError, ValueError):
                prior_runs = []
        results = []
        with tempfile.TemporaryDirectory(prefix="v4-pipeline-fixtures-") as temp_name:
            temp_root = Path(temp_name)
            fixture_root = FIXTURES
            if not all((fixture_root / name).is_file() for name in NAMES):
                fixture_root = temp_root / "generated-fixtures"
                make_corpus(fixture_root)
            font_dir = temp_root / "fonts"
            font_dir.mkdir()
            (font_dir / "cjk.ttf").write_bytes(pymupdf.Font("cjk").buffer)

            for name in NAMES:
                fixture_started_utc = utc_now()
                clock = time.perf_counter()
                record = {"fixture": name, "source_kind": "synthetic", "status": "failed"}
                try:
                    source = fixture_root / name
                    if not source.is_file():
                        raise FileNotFoundError(f"Generated fixture missing: {source}")
                    run_dir = temp_root / source.stem
                    mapping, _jobs = prepare(source, run_dir, config={"provider": "host"}, extractor="local")

                    # Simulate a human reviewing and accepting the prepared map.
                    mapping["layout_reviewed"] = True
                    # These synthetic generator-owned annotations are reviewed
                    # specifically: table caption is prose next to the known
                    # synthetic table, bibliography continuation is its disclaimer.
                    decisions={'caption-candidate-without-geometry':'Synthetic table caption retained as prose; native table remains protected.',
                               'bibliography-continuation-candidate':'Generator disclaimer continuation preserved in bibliography area.'}
                    mapping['review_resolutions']={i.get('id','')+'|'+i['check']+'|'+str(i.get('page','')):decisions[i['check']]
                                                  for i in mapping.get('issues',[]) if i['check'] in decisions}
                    write(run_dir / "source_map.json", mapping)

                    # Replace prose words with fixed Chinese mock text while
                    # retaining source separators and every frozen token.
                    translations = {}
                    for block in mapping["blocks"]:
                        if not block.get("translation_required", True) or block.get("translation_in"):
                            continue
                        # A cross-box paragraph is one translation of the merged source.
                        translations[block["id"]] = mock_translation(block.get("logical_source") or block["source_text"])
                    translations_path = run_dir / "mock-translations.json"
                    write(translations_path, translations)

                    output = run_dir / "translated.pdf"
                    report, validation = finish(
                        source,
                        run_dir,
                        output,
                        font_dir,
                        translations=translations_path,
                        config={"provider": "host"},
                        reviewed=True,
                        fonts={"regular": "cjk.ttf", "bold": "cjk.ttf", "fallback": "cjk.ttf"},
                    )
                    if not output.is_file() or output.stat().st_size == 0:
                        raise AssertionError("finish returned without a nonempty output PDF")
                    if not validation.get("passed"):
                        raise AssertionError(f"Functional validation failed: {validation}")
                    record.update(
                        status="passed",
                        source_pages=len(mapping.get("pages", [])),
                        block_count=len(mapping.get("blocks", [])),
                        translated_block_count=len(translations),
                        output_bytes=output.stat().st_size,
                        output_pages=report.get("total_pages"),
                        structural_checks_passed=report.get("structural_checks_passed"),
                        functional_validation_passed=validation.get("passed"),
                        protected_regions=len(report.get("protected", [])),
                        references=len(mapping.get("references", [])),
                    )
                except Exception as exc:  # retain details for every fixture, then fail the suite
                    record.update(
                        error_type=type(exc).__name__,
                        error=str(exc),
                        traceback=traceback.format_exc(),
                    )
                    match = re.search(r"see (.+preflight\.json)", str(exc))
                    if match:
                        diagnostic_path = Path(match.group(1))
                        if diagnostic_path.is_file():
                            try:
                                diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
                                record["preflight_diagnostic"] = {
                                    "issue_count": len(diagnostic.get("issues", [])),
                                    "issues": diagnostic.get("issues", []),
                                }
                            except (OSError, ValueError) as diagnostic_error:
                                record["preflight_diagnostic_error"] = str(diagnostic_error)
                finally:
                    record["started_utc"] = fixture_started_utc
                    record["ended_utc"] = utc_now()
                    record["elapsed_seconds"] = round(time.perf_counter() - clock, 3)
                    results.append(record)

        completed = utc_now()
        report_path = REPORT
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # A checkout that is not nested under the author's workspace still
            # records the run; the fixture assertions do not depend on that path.
            report_path = Path(tempfile.gettempdir()) / "v4-upgrade" / "pipeline-fixtures-report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
        current_run = {
            "started_utc": started,
            "ended_utc": completed,
            "elapsed_seconds": round(sum(item["elapsed_seconds"] for item in results), 3),
            "fixtures": results,
            "summary": {
                "passed": sum(item["status"] == "passed" for item in results),
                "failed": sum(item["status"] != "passed" for item in results),
            },
        }
        report_path.write_text(
            json.dumps(
                {
                    "test": "prepare → simulated source-map review → frozen-token Chinese mock translations → finish",
                    "source_kind": "synthetic",
                    "fixture_directory": "work/v4-upgrade/generated-fixtures",
                    "previous_runs": prior_runs,
                    "runs": [*prior_runs, current_run],
                    **current_run,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        failures = [item for item in results if item["status"] != "passed"]
        self.assertFalse(
            failures,
            "Pipeline fixture failures (full tracebacks are in "
            f"{report_path}):\n"
            + "\n".join(
                f"{item['fixture']}: {item.get('error_type')}: {item.get('error')}"
                for item in failures
            ),
        )


if __name__ == "__main__":
    unittest.main()
