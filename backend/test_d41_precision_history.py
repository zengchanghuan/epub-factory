"""R3 historical precision-polish protocol and artifact-safety regression.

Opt in with EPUB_HISTORY_UPLOAD_DIR and EPUB_HISTORY_OUTPUT_DIR. The three
SHA-pinned historical originals are actually converted, scanned, polished and
EPUBChecked locally. Only model responses are controlled; these tests do not
claim to assess a real model's semantic quality or exercise payment/refunds.
Original uploads and existing delivered files remain read-only.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import unittest
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from contextlib import redirect_stdout
from unittest.mock import patch

import test_d39_table_history as tables


# Measured on these SHA-pinned originals' spine paragraphs (including notes),
# not an assumed count for arbitrary books or evidence of needed corrections.
NATURAL_CANDIDATES = {"double-helix": 0, "die-with-zero": 58,
                      "responsibility-and-judgement": 57}


def archive_bytes(path):
    """Independent ZIP inspection, not the production updater/parser."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise AssertionError("Duplicate ZIP member in precision artifact")
        return {name: archive.read(name) for name in names}


def member_for(members, logical_name):
    names = [name for name in members if name == logical_name or name.endswith("/" + logical_name)]
    if len(names) != 1:
        raise AssertionError(f"Expected one historical ZIP member: {logical_name}")
    return names[0]


def xml_signature(node):
    """All element order, attributes, text and tails; no lossy prose normalization."""
    return (node.tag, tuple(sorted(node.attrib.items())), node.text or "",
            tuple(xml_signature(child) for child in node), node.tail or "")


def xml_structure(node):
    """Compare markup exactly while allowing explicitly enabled L1/L2/L3 prose."""
    return (node.tag, tuple(sorted(node.attrib.items())),
            tuple(xml_structure(child) for child in node))


def expected_single_text_edit(raw, old, new):
    """Construct an independent exact allowlist on an actual source text node."""
    root = ET.fromstring(raw)
    changed = 0
    for node in root.iter():
        for field in ("text", "tail"):
            value = getattr(node, field)
            if value and old in value:
                changed += value.count(old)
                setattr(node, field, value.replace(old, new))
    if changed != 1:
        raise AssertionError(f"Controlled real-text edit must be unique, found {changed}")
    return xml_signature(root)


def visible_paragraphs(members):
    """Read actual visible paragraph text independently for response provenance."""
    result = []
    for name, raw in members.items():
        if not name.lower().endswith((".xhtml", ".html", ".htm")):
            continue
        root = ET.fromstring(raw)
        for index, node in enumerate(element for element in root.iter()
                                     if element.tag.rsplit("}", 1)[-1] == "p"):
            text = "".join(node.itertext())
            result.append({"member": name, "index": index, "text": text,
                           "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    return result


class PrecisionHistoryTests(tables.TableHistoryTests):
    @classmethod
    def setUpClass(cls):
        # Before any model module import, remove ambient provider credentials.
        environment = patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "", "OPENAI_API_KEY": "", "DASHSCOPE_API_KEY": "",
            "DEEPSEEK_MODEL": "deepseek-flash", "SMTP_HOST": "",
        })
        environment.start()
        cls.addClassCleanup(environment.stop)
        # app.engine.__init__ imports compiler, whose module-level dotenv load
        # otherwise runs even while resolving a patch target for LLMPolisher.
        dotenv_guard = patch("dotenv.load_dotenv", return_value=False)
        dotenv_guard.start()
        cls.addClassCleanup(dotenv_guard.stop)
        with patch("app.engine.cleaners.llm_polish.LLMPolisher.__init__",
                   side_effect=AssertionError("Precision-off conversion constructed a polisher")) as disabled:
            super().setUpClass()
        cls.disabled_constructor_calls = disabled.call_count

        from sqlalchemy import create_engine
        from app.domain import precision_polish_service
        from app.engine.cleaners.llm_polish import LLMPolisher
        from app.engine.compiler import EPUBCHECK_JAR
        from app.engine.epub_validation import validate_epub

        cls.api = precision_polish_service
        cls.real_polisher = LLMPolisher
        cls.jar = EPUBCHECK_JAR
        cls.validate = staticmethod(validate_epub)
        cls.ledger_engine = create_engine("sqlite:///" + str(cls.root / "precision-ledger.sqlite3"))
        cls.addClassCleanup(cls.ledger_engine.dispose)
        cls.case_sequence = 0
        cls.prepared_hashes = {}
        cls.precision_records = {}
        cls.inventory = {}
        cls.members = {}
        cls.stack.enter_context(patch("app.engine.cleaners.llm_polish.time.sleep", return_value=None))

        for key, (original, prepared, compiler) in list(cls.runs.items()):
            path = cls.root / (key + "-converted.epub")
            cls.prepared_hashes[key] = tables.navigation.sha256(path)
            cls.members[key] = archive_bytes(path)
            cls.inventory[key] = visible_paragraphs(cls.members[key])
            output = cls.root / (key + "-precision-keep.epub")
            trace = cls.new_trace(key)
            try:
                stats = cls.run_precision(key, output, trace)
            except cls.api.PrecisionPolishError as error:
                print("R3 historical setup gate failed: " + json.dumps({
                    "book": key, "reason": error.reason, "stats": error.stats,
                    "controlled_requests": len(trace["requests"]),
                    "epubcheck_errors": {name: [message for message in report.get("messages", [])
                        if message.get("severity") in {"ERROR", "FATAL"}]
                        for name, report in cls.validation_reports.items()
                        if any(message.get("severity") in {"ERROR", "FATAL"}
                               for message in report.get("messages", []))},
                }, ensure_ascii=False, sort_keys=True))
                raise
            checked = cls.validate(output, cls.jar)
            cls.precision_records[key] = {"output": output, "stats": stats, "trace": trace, "validation": checked}
            cls.runs[key] = (original, tables.navigation.BookSnapshot(output, cls.opencc), compiler)

        print("R3 historical controlled-response counts: " + json.dumps({key: {
            "requests": len(record["trace"]["requests"]),
            "stats": record["stats"],
        } for key, record in cls.precision_records.items()}, ensure_ascii=False, sort_keys=True))

    @classmethod
    def new_trace(cls, key, *, edit=False, invalid=None, fail_after_edit=False):
        return {"book": key, "requests": [], "edit": edit, "invalid": invalid,
                "fail_after_edit": fail_after_edit, "edits": 0, "ledger_job": None,
                "callbacks": [], "factory_calls": 0}

    @classmethod
    def response(cls, payload, trace):
        request = json.loads(payload["messages"][-1]["content"])
        if not isinstance(request, dict) or not isinstance(request.get("occurrences"), list):
            raise AssertionError("Model input did not contain occurrence decisions")
        context = request["context"]
        if not isinstance(context, str):
            raise AssertionError("Expected a visible historical paragraph as model context")
        # Match real source paragraphs, not a synthetic stand-in or invented excerpt.
        actual = [paragraph for paragraph in cls.inventory[trace["book"]]
                  if paragraph["text"] == context]
        if not actual:
            raise AssertionError("Precision request context is not an actual historical paragraph")
        occurrence_ids = [item["id"] for item in request["occurrences"]]
        if len(occurrence_ids) != len(set(occurrence_ids)):
            raise AssertionError("Duplicate occurrence IDs before the model call")
        trace["requests"].append(copy.deepcopy(request))

        if trace["fail_after_edit"] and trace["edits"]:
            content = "not a valid decision JSON response"
        else:
            decisions = []
            for item in request["occurrences"]:
                source = item["source"]
                if source not in context:
                    raise AssertionError("Risk source does not exist in the historical paragraph")
                replacement = source
                if (trace["edit"] and source == "搞" and item.get("editable", True)
                        and "被搞得头昏脑胀" in context and not trace["edits"]):
                    replacement = "弄"
                    trace["edits"] += 1
                if trace["invalid"] and not decisions:
                    replacement = source + ("2026" if trace["invalid"] == "digits" else "<em>追加</em>")
                decisions.append({"id": item["id"], "source": source,
                                  "action": "keep" if replacement == source else "replace",
                                  "replacement": replacement})
            content = json.dumps({"decisions": decisions}, ensure_ascii=False)
        return {"id": "offline-history-" + str(len(trace["requests"])),
                "model": payload["model"],
                "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
                          "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 11}}

    @classmethod
    def run_precision(cls, key, output, trace, *, cancel_check=None):
        from app.infra.llm_usage_ledger import usage_scope

        class ControlledTransport(cls.real_polisher):
            def _request(self, payload):
                return cls.response(payload, trace)

        def factory():
            trace["factory_calls"] += 1
            return ControlledTransport(api_key="offline-not-a-real-credential",
                                       base_url="https://api.deepseek.com/v1", model="deepseek-flash")

        cls.case_sequence += 1
        trace["ledger_job"] = "d41-" + str(cls.case_sequence)
        with usage_scope(trace["ledger_job"], "precision", engine=cls.ledger_engine):
            return cls.api.run_precision_polish(
                cls.root / (key + "-converted.epub"), output,
                cancel_check=cancel_check,
                stats_callback=lambda stats: trace["callbacks"].append(copy.deepcopy(stats)),
                polisher_factory=factory,
            )

    def assert_zero_epubcheck_errors(self, output, result):
        self.assertTrue(result.passed, result.message)
        report = self.validation_reports[output.name]
        self.assertEqual(report["checker"]["nError"], 0)
        self.assertEqual(report["checker"]["nFatal"], 0)
        self.assertFalse([message for message in report["messages"]
                          if message["severity"] in {"ERROR", "FATAL"}])

    def test_actual_epubcheck_fixes_target_without_new_errors_elsewhere(self):
        super().test_actual_epubcheck_fixes_target_without_new_errors_elsewhere()
        for key, record in self.precision_records.items():
            with self.subTest(book=key):
                self.assert_zero_epubcheck_errors(record["output"], record["validation"])

    def test_disabled_control_and_natural_no_candidate_book_never_call_a_model(self):
        self.assertEqual(self.disabled_constructor_calls, 0)
        record = self.precision_records["double-helix"]
        self.assertEqual(record["stats"]["status"], "no_candidates")
        self.assertEqual(record["stats"]["candidates"], 0)
        self.assertEqual(record["trace"]["requests"], [])
        self.assertEqual(record["trace"]["factory_calls"], 0)

    def test_raw_upload_quote_matches_real_conversion_without_model_calls(self):
        parity = {}
        for key in self.precision_records:
            with self.subTest(book=key):
                original = self.root / (key + "-source.epub")
                converted = self.root / (key + "-converted.epub")
                before = tables.navigation.sha256(original)
                with patch.object(self.real_polisher, "__init__",
                                  side_effect=AssertionError("Quote inspection must be free of model calls")) as constructor:
                    # Match the real compiler configuration inherited from D38,
                    # not a default dictionary configuration used only in quote.
                    result = self.api.inspect_precision_polish_source(
                        original, traditional_variant="auto", lexicon_domains=[],
                        enable_proper_noun=False,
                    )
                    source_plans = self.api._load_plans(original)
                    execution_plans = self.api._load_plans(converted)
                constructor.assert_not_called()
                execution_candidates = sum(len(plan.paragraphs) for plan in execution_plans.values())
                original_char_count = sum(plan.char_count for plan in source_plans.values())
                self.assertGreater(result["char_count"], 0)
                self.assertGreater(result["paragraphs_scanned"], 0)
                self.assertEqual(result["char_count"], original_char_count,
                                 "Quote preview changed the original manuscript's pricing character count")
                self.assertEqual(result["candidates"], execution_candidates,
                                 "Quoted candidates differ from actual converted EPUB review scope")
                self.assertEqual(result["candidates"], self.precision_records[key]["stats"]["candidates"],
                                 "Quote preview and actual precision run reviewed different scopes")
                self.assertEqual(execution_candidates, NATURAL_CANDIDATES[key])
                self.assertEqual(tables.navigation.sha256(original), before)
                parity[key] = {"quote_candidates": result["candidates"],
                               "execution_candidates": execution_candidates,
                               "original_char_count": original_char_count}
        print("R3 historical quote/execution parity: " + json.dumps(parity, sort_keys=True))

    def test_real_noop_keeps_every_member_byte_identical_and_reports_truthfully(self):
        for key, record in self.precision_records.items():
            with self.subTest(book=key):
                self.assertEqual(archive_bytes(record["output"]), self.members[key])
                stats = record["stats"]
                self.assertEqual(stats["candidates"], NATURAL_CANDIDATES[key])
                self.assertEqual(stats["changed"], 0)
                self.assertEqual(stats["reviewed"], len(record["trace"]["requests"]))
                self.assertEqual(stats["unchanged"], stats["reviewed"])
                if key != "double-helix":
                    self.assertEqual(stats["status"], "completed")
                    self.assertGreater(stats["reviewed"], 0)
                self.assertEqual(stats["candidates"], stats["reviewed"])

    def test_production_default_dictionary_quote_conversion_parity_for_auto_and_tw(self):
        from app.converter import EpubConverter
        from app.models import OutputMode

        combinations = [(key, "auto") for key in self.precision_records]
        combinations += [(key, "tw") for key in ("die-with-zero", "responsibility-and-judgement")]
        parity = []
        for key, variant in combinations:
            with self.subTest(book=key, variant=variant):
                source = self.root / (key + "-source.epub")
                source_hash = tables.navigation.sha256(source)
                output = self.root / ("production-default-" + key + "-" + variant + ".epub")
                with patch.object(self.real_polisher, "__init__",
                                  side_effect=AssertionError("Quote/default conversion must not invoke a model")) as constructor:
                    # Deliberately leave lexicon_domains and proper_noun unset:
                    # these are the product's real default L2/L3 settings.
                    quote = self.api.inspect_precision_polish_source(source, traditional_variant=variant)
                    with redirect_stdout(io.StringIO()):
                        converted = EpubConverter().convert_file_to_horizontal(
                            source, output, OutputMode.simplified,
                            enable_translation=False, traditional_variant=variant,
                        )
                    plans = self.api._load_plans(output)
                    original_plans = self.api._load_plans(source)
                constructor.assert_not_called()
                self.assertTrue(converted.validation_passed, converted.message)
                report = self.validation_reports[output.name]
                self.assertEqual(report["checker"]["nError"], 0)
                self.assertEqual(report["checker"]["nFatal"], 0)
                self.assertFalse([message for message in report["messages"]
                                  if message["severity"] in {"ERROR", "FATAL"}])
                candidates = sum(len(plan.paragraphs) for plan in plans.values())
                self.assertEqual(quote["candidates"], candidates,
                                 "Production-default quote and actual converter selected different risk scopes")
                self.assertEqual(quote["char_count"], sum(plan.char_count for plan in original_plans.values()))

                # Dictionary conversions legitimately change prose. They must
                # not change images, DOM structure/attributes, anchors or links.
                before = self.runs[key][1]
                after = tables.navigation.BookSnapshot(output, self.opencc)
                self.assertEqual(before.images, after.images)
                self.assertEqual(set(before.docs), set(after.docs))
                produced_members = archive_bytes(output)
                for name, baseline in before.docs.items():
                    current = after.docs[name]
                    self.assertEqual(baseline["ids"], current["ids"], f"Default dictionaries changed anchors: {name}")
                    def links(document):
                        return Counter((link["href"], link["target"], link["disabled"])
                                       for link in document["links"])
                    self.assertEqual(links(baseline), links(current),
                                     f"Default dictionaries changed link/footnote destinations: {name}")
                    baseline_member = member_for(self.members[key], name)
                    produced_member = member_for(produced_members, name)
                    self.assertEqual(
                        xml_structure(ET.fromstring(self.members[key][baseline_member])),
                        xml_structure(ET.fromstring(produced_members[produced_member])),
                        f"Default dictionaries changed original markup structure/attributes: {name}",
                    )
                self.assertEqual([(entry["target"], entry["depth"]) for entry in before.toc],
                                 [(entry["target"], entry["depth"]) for entry in after.toc])
                self.assertEqual(tables.navigation.sha256(source), source_hash)
                parity.append({"book": key, "variant": variant, "quote_candidates": quote["candidates"],
                               "execution_candidates": candidates, "char_count": quote["char_count"],
                               "epubcheck_errors": report["checker"]["nError"],
                               "epubcheck_fatal": report["checker"]["nFatal"]})
        self.assertEqual(len(parity), 5)
        print("R3 production-default real-book quote/conversion parity: " + json.dumps(parity, sort_keys=True))

    def test_controlled_actual_risk_span_edit_has_only_one_allowed_text_delta(self):
        key = "die-with-zero"
        trace = self.new_trace(key, edit=True)
        output = self.root / "real-one-span-precision.epub"
        stats = self.run_precision(key, output, trace)
        self.assertEqual(trace["edits"], 1, "Real eligible 搞 occurrence was not exercised")
        self.assertEqual(stats["status"], "completed")
        self.assertEqual(stats["changed"], 1)
        self.assertEqual(stats["reviewed"], stats["changed"] + stats["unchanged"])
        before, after = self.members[key], archive_bytes(output)
        self.assertEqual(set(before), set(after))
        member = member_for(before, "xhtml/p-007.xhtml")
        for name in before:
            with self.subTest(member=name):
                if name == member:
                    expected = expected_single_text_edit(before[name], "被搞得头昏脑胀", "被弄得头昏脑胀")
                    self.assertEqual(xml_signature(ET.fromstring(after[name])), expected,
                                     "Model affected non-authorized text/HTML/IDs/links")
                else:
                    self.assertEqual(before[name], after[name], "Unrelated ZIP member changed")
        self.assert_zero_epubcheck_errors(output, self.validate(output, self.jar))

    def test_real_usage_ledger_contains_only_controlled_precision_requests(self):
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from app.infra.llm_usage_ledger import UsageRequest

        with Session(self.ledger_engine) as session:
            for key, record in self.precision_records.items():
                rows = list(session.scalars(select(UsageRequest).where(
                    UsageRequest.job_id == record["trace"]["ledger_job"])))
                self.assertEqual(len(rows), len(record["trace"]["requests"]))
                for row in rows:
                    self.assertEqual(row.stage, "precision_polish")
                    self.assertEqual(row.requested_model, "deepseek-flash")
                    self.assertEqual(row.prompt_tokens, 11)
                    self.assertEqual(row.completion_tokens, 7)
                    self.assertEqual(row.total_tokens, 18)
                    self.assertEqual(row.usage_status, "complete")

    def test_invalid_numeric_or_html_response_cannot_publish_an_artifact(self):
        for invalid in ("digits", "html"):
            with self.subTest(invalid=invalid):
                directory = self.root / ("rejected-" + invalid)
                directory.mkdir()
                output = directory / "must-not-exist.epub"
                trace = self.new_trace("die-with-zero", invalid=invalid)
                with self.assertRaises(self.api.PrecisionPolishError) as caught:
                    self.run_precision("die-with-zero", output, trace)
                self.assertTrue(trace["requests"], "No actual real paragraph reached model protocol QA")
                self.assertNotIn(caught.exception.stats.get("status"), {"completed", "no_candidates"})
                self.assertEqual(list(directory.iterdir()), [], "Rejected output or partial staging file leaked")

    def test_failure_after_accepted_edit_cannot_publish_partial_success(self):
        directory = self.root / "failed-after-edit"
        directory.mkdir()
        output = directory / "must-not-exist.epub"
        trace = self.new_trace("die-with-zero", edit=True, fail_after_edit=True)
        with self.assertRaises(self.api.PrecisionPolishError) as caught:
            self.run_precision("die-with-zero", output, trace)
        self.assertEqual(trace["edits"], 1)
        self.assertGreater(len(trace["requests"]), 1)
        self.assertNotIn(caught.exception.stats.get("status"), {"completed", "no_candidates"})
        self.assertEqual(list(directory.iterdir()), [])

    def test_cancellation_after_a_real_request_preserves_previous_artifact(self):
        import shutil
        directory = self.root / "cancelled"
        directory.mkdir()
        previous = directory / "previous-success.epub"
        output = directory / "new-attempt-must-not-exist.epub"
        shutil.copyfile(self.root / "die-with-zero-converted.epub", previous)
        before = tables.navigation.sha256(previous)
        trace = self.new_trace("die-with-zero")
        def check_cancelled():
            if trace["requests"]:
                raise self.api.PrecisionPolishError("cancelled", "Offline controlled cancellation")
        with self.assertRaises(self.api.PrecisionPolishError) as caught:
            self.run_precision("die-with-zero", output, trace,
                               cancel_check=check_cancelled)
        self.assertTrue(trace["requests"])
        self.assertNotIn(caught.exception.stats.get("status"), {"completed", "no_candidates"})
        self.assertEqual(tables.navigation.sha256(previous), before)
        self.assertEqual(list(directory.iterdir()), [previous])

    def test_boolean_runner_cancellation_stops_after_first_real_request(self):
        from app.cancellation import JobCancelled

        directory = self.root / "cancelled-boolean-runner"
        directory.mkdir()
        output = directory / "must-not-exist.epub"
        trace = self.new_trace("die-with-zero")
        with self.assertRaises(JobCancelled):
            self.run_precision("die-with-zero", output, trace,
                               cancel_check=lambda: bool(trace["requests"]))
        self.assertEqual(len(trace["requests"]), 1,
                         "Runner's boolean cancellation was ignored and more requests were sent")
        self.assertFalse([stats for stats in trace["callbacks"]
                          if stats.get("status") in {"completed", "no_candidates"}])
        self.assertEqual(list(directory.iterdir()), [])

    @classmethod
    def tearDownClass(cls):
        for key, expected in cls.prepared_hashes.items():
            if tables.navigation.sha256(cls.root / (key + "-converted.epub")) != expected:
                raise AssertionError("Precision service modified its prepared input")
        super().tearDownClass()


if __name__ == "__main__":
    unittest.main(verbosity=2)
