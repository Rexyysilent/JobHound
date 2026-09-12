#!/usr/bin/env python3
"""Offline acceptance evaluator, not a JobHound implementation.

contracts: checks outputs exported by a real JobHound fixture adapter against
           acceptance_cases.jsonl. This program does not synthesize outputs.
quality:   measures a human-labeled candidate audit. Labels and evidence must
           be independently inspected; this program cannot authenticate them.
self-test: tests this evaluator only, never the actual JobHound pipeline.

Standard library only. Reads input files and optionally writes one report.
Does not use network, production databases, environment secrets, or mail.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import unittest
from pathlib import Path
from typing import Any

MISSING = object()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name}:{line_number}: invalid JSON") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path.name}:{line_number}: expected JSON object")
            rows.append(row)
    if not rows:
        raise ValueError(f"{path.name}: empty input cannot pass acceptance")
    return rows


def at_path(value: Any, path: str) -> Any:
    """Resolve dot-separated object keys or numeric list indices."""
    if not isinstance(path, str) or not path:
        raise ValueError("Assertion path must be a nonempty string")
    current = value
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return MISSING
    return current


def strict_equal(left: Any, right: Any) -> bool:
    # JSON true must not accidentally equal JSON 1.
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return left == right


def assertion_passes(actual: dict[str, Any], assertion: dict[str, Any]) -> bool:
    path, op = assertion.get("path"), assertion.get("op")
    observed = at_path(actual, path)
    expected = assertion.get("value")
    if op == "exists":
        return observed is not MISSING
    if op == "missing":
        return observed is MISSING
    if observed is MISSING:
        return False  # Missing is not null and cannot satisfy a negative assertion.
    if op == "eq":
        return strict_equal(observed, expected)
    if op == "ne":
        return not strict_equal(observed, expected)
    if op == "is_null":
        return observed is None
    if op == "not_null":
        return observed is not None
    if op in {"in", "not_in"}:
        if not isinstance(expected, list):
            raise ValueError(f"{path}: '{op}' needs a list value")
        found = any(strict_equal(observed, value) for value in expected)
        return found if op == "in" else not found
    if op in {"contains", "not_contains"}:
        if not isinstance(observed, (list, str, dict)):
            return False
        found = expected in observed
        return found if op == "contains" else not found
    if op in {"gte", "lte"}:
        if any(isinstance(x, bool) or not isinstance(x, (int, float))
               or not math.isfinite(x) for x in (observed, expected)):
            return False
        return observed >= expected if op == "gte" else observed <= expected
    raise ValueError(f"{path}: unsupported assertion operation '{op}'")


def unique_rows(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        ident = row.get(key)
        if not isinstance(ident, str) or not ident:
            raise ValueError(f"Every row needs a nonempty string '{key}'")
        if ident in indexed:
            raise ValueError(f"Duplicate {key}: {ident}")
        indexed[ident] = row
    return indexed


def evaluate_contracts(cases: list[dict[str, Any]], outputs: list[dict[str, Any]]) -> dict[str, Any]:
    expected = unique_rows(cases, "id")
    supplied = unique_rows(outputs, "case_id")
    if not expected:
        raise ValueError("No contract cases supplied")
    failures: list[dict[str, Any]] = []
    missing_ids = sorted(set(expected) - set(supplied))
    extra_ids = sorted(set(supplied) - set(expected))
    assertion_count = 0
    for case_id, case in expected.items():
        assertions = case.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise ValueError(f"{case_id}: no assertions")
        assertion_count += len(assertions)
        if case_id not in supplied:
            continue
        actual = supplied[case_id].get("actual")
        if not isinstance(actual, dict):
            raise ValueError(f"{case_id}: output needs object 'actual'")
        for assertion in assertions:
            if not isinstance(assertion, dict):
                raise ValueError(f"{case_id}: invalid assertion")
            if not assertion_passes(actual, assertion):
                # Do not echo raw actual values: exported outputs may need redaction.
                failures.append({"case_id": case_id, "path": assertion.get("path"),
                                 "op": assertion.get("op")})
    passed = not (missing_ids or extra_ids or failures)
    return {"evaluator_version": "1.0", "mode": "contracts",
            "passed": passed, "cases": len(expected), "assertions": assertion_count,
            "missing_output_ids": missing_ids, "unexpected_output_ids": extra_ids,
            "failed_assertions": failures,
            "scope": "Assertions over supplied outputs only; no engine execution or certification."}


def ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def evaluate_quality(review: dict[str, Any]) -> dict[str, Any]:
    if review.get("schema_version") != "jobhound.review.v1":
        raise ValueError("Expected schema_version jobhound.review.v1")
    kind = review.get("corpus_kind")
    if kind not in {"repair", "holdout", "live", "historical"}:
        raise ValueError("corpus_kind must be repair, holdout, live, or historical")
    presentation_mode = review.get("presentation_mode")
    if presentation_mode not in {"fresh_preview", "notification_delta"}:
        raise ValueError("presentation_mode must be fresh_preview or notification_delta")
    records = review.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("A nonempty records list is required")
    if not all(isinstance(row, dict) for row in records):
        raise ValueError("Every record must be an object")
    indexed_records = unique_rows(records, "id")
    manifest_ids = review.get("corpus_record_ids")
    if (not isinstance(manifest_ids, list) or not manifest_ids
            or not all(isinstance(x, str) and x for x in manifest_ids)
            or len(manifest_ids) != len(set(manifest_ids))):
        raise ValueError("corpus_record_ids must be a nonempty unique ID list frozen before review")
    failures: list[str] = []
    if set(manifest_ids) != set(indexed_records):
        failures.append("review_does_not_cover_frozen_corpus_ids")
    unresolved = 0
    ranks: dict[str, list[int]] = {"primary": [], "verify": []}
    for row in records:
        ident = row["id"]
        if row.get("gold_band") not in {"primary", "verify", "reject", "watch", "unresolved"}:
            raise ValueError(f"{ident}: invalid gold_band")
        if row.get("selected_band") not in {"primary", "verify", "reject"}:
            raise ValueError(f"{ident}: invalid selected_band")
        section = row.get("display_section")
        if section not in {None, "primary", "verify"}:
            raise ValueError(f"{ident}: invalid display_section")
        critical = row.get("critical_errors")
        if not isinstance(critical, list) or not all(isinstance(x, str) for x in critical):
            raise ValueError(f"{ident}: critical_errors must be an explicit string list")
        if critical:
            failures.append(f"critical_error:{ident}")
        refs = row.get("source_refs")
        if not isinstance(refs, list) or not refs or not all(isinstance(x, str) and x for x in refs):
            failures.append(f"missing_label_evidence:{ident}")
        if row["gold_band"] == "unresolved":
            unresolved += 1
        if section:
            if row["selected_band"] != section:
                failures.append(f"band_display_mismatch:{ident}")
            rank = row.get("display_rank")
            if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
                raise ValueError(f"{ident}: displayed item requires positive integer display_rank")
            ranks[section].append(rank)
            claims, supported = row.get("material_claims"), row.get("supported_material_claims")
            if any(isinstance(x, bool) or not isinstance(x, int) for x in (claims, supported)):
                raise ValueError(f"{ident}: claim counts must be explicit integers")
            if claims <= 0 or supported < 0 or supported > claims:
                raise ValueError(f"{ident}: invalid claim counts")
            if claims != supported:
                failures.append(f"unsupported_displayed_claim:{ident}")
            if row["gold_band"] == "unresolved":
                failures.append(f"unresolved_displayed_label:{ident}")
        if section == "verify":
            if not isinstance(row.get("verify_useful"), bool):
                raise ValueError(f"{ident}: Verify requires boolean verify_useful")
            if row.get("verification_task_complete") is not True:
                failures.append(f"incomplete_verification_task:{ident}")
    for section, section_ranks in ranks.items():
        if sorted(section_ranks) != list(range(1, len(section_ranks) + 1)):
            failures.append(f"noncontiguous_or_duplicate_display_ranks:{section}")
    if len(ranks["primary"]) > 5 or len(ranks["verify"]) > 3:
        failures.append("display_cap_exceeded")
    primary = [r for r in records if r["selected_band"] == "primary"]
    gold_primary = [r for r in records if r["gold_band"] == "primary"]
    correct_primary = [r for r in primary if r["gold_band"] == "primary" and not r["critical_errors"]]
    verify_shown = [r for r in records if r.get("display_section") == "verify"]
    useful_verify = [r for r in verify_shown if r["verify_useful"] and r["gold_band"] == "verify" and not r["critical_errors"]]
    top = [r for r in records if r.get("display_section") == "primary" and r["display_rank"] <= 5]
    if presentation_mode == "fresh_preview" and gold_primary and not top:
        failures.append("no_primary_display_despite_gold_primary_controls")
    if any(r["gold_band"] != "primary" or r["critical_errors"] for r in top):
        failures.append("top_primary_not_appropriate")
    precision = ratio(len(correct_primary), len(primary))
    recall = ratio(len(correct_primary), len(gold_primary))
    verify_precision = ratio(len(useful_verify), len(verify_shown))
    if precision is not None and precision < 0.95:
        failures.append("primary_precision_below_0.95")
    if recall is not None and recall < 0.90:
        failures.append("primary_retention_below_0.90")
    if verify_precision is not None and verify_precision < 0.80:
        failures.append("useful_verify_fraction_below_0.80")
    insufficient: list[str] = []
    if not gold_primary:
        insufficient.append("no_gold_primary_controls")
    if unresolved:
        insufficient.append("unresolved_gold_labels")
    if review.get("independent_review_attested") is not True:
        insufficient.append("independent_review_not_attested")
    if kind == "holdout":
        if len(records) < 100:
            insufficient.append("holdout_smaller_than_100")
        if len(gold_primary) < 20:
            insufficient.append("fewer_than_20_gold_primary_holdout_controls")
    passed = not failures
    adequate = passed and not insufficient
    if not passed:
        status = "FAIL"
    elif kind == "live" and not gold_primary and not primary:
        status = "VALID_EMPTY_READY_SAMPLE"
    elif insufficient:
        status = "INSUFFICIENT_EVIDENCE"
    else:
        status = "SAMPLE_GATES_PASS"
    return {"evaluator_version": "1.0", "mode": "quality", "corpus_kind": kind,
            "status": status, "passed": passed, "adequate_sample_evidence": adequate,
            "record_count": len(records), "unresolved_labels": unresolved,
            "primary_precision": {"numerator": len(correct_primary), "denominator": len(primary), "value": precision},
            "primary_retention": {"numerator": len(correct_primary), "denominator": len(gold_primary), "value": recall},
            "useful_verify_fraction": {"numerator": len(useful_verify), "denominator": len(verify_shown), "value": verify_precision},
            "failures": sorted(set(failures)), "insufficient_evidence": insufficient,
            "scope": "Only supplied review labels; does not prove review independence, global recall, source truth, live safety, or production approval."}


class EvaluatorTests(unittest.TestCase):
    @staticmethod
    def row(**updates: Any) -> dict[str, Any]:
        value = {"id": "test", "gold_band": "primary", "selected_band": "primary",
                 "display_section": "primary", "display_rank": 1,
                 "critical_errors": [], "source_refs": ["synthetic/self-test"],
                 "material_claims": 2, "supported_material_claims": 2}
        value.update(updates)
        return value

    @staticmethod
    def review(*rows: dict[str, Any], kind: str = "repair") -> dict[str, Any]:
        return {"schema_version": "jobhound.review.v1", "corpus_kind": kind,
                "independent_review_attested": True, "presentation_mode": "fresh_preview", "corpus_record_ids": [r["id"] for r in rows], "records": list(rows)}

    def test_missing_is_not_null(self):
        self.assertFalse(assertion_passes({}, {"path": "a", "op": "is_null"}))
        self.assertTrue(assertion_passes({"a": None}, {"path": "a", "op": "is_null"}))

    def test_bool_is_not_one(self):
        self.assertFalse(assertion_passes({"a": True}, {"path": "a", "op": "eq", "value": 1}))

    def test_contract_missing_and_extra(self):
        cases = [{"id": "one", "assertions": [{"path": "x", "op": "eq", "value": 3}]}]
        out = [{"case_id": "two", "actual": {"x": 3}}]
        self.assertFalse(evaluate_contracts(cases, out)["passed"])

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError):
            unique_rows([{"id": "x"}, {"id": "x"}], "id")

    def test_contract_success_and_failure(self):
        cases = [{"id": "one", "assertions": [{"path": "pay.basis", "op": "eq", "value": "output_audio_hour"}]}]
        self.assertTrue(evaluate_contracts(cases, [{"case_id": "one", "actual": {"pay": {"basis": "output_audio_hour"}}}])["passed"])
        self.assertFalse(evaluate_contracts(cases, [{"case_id": "one", "actual": {"pay": {"basis": "labor_hour"}}}])["passed"])

    def test_all_reject_fails_recall(self):
        result = evaluate_quality(self.review(self.row(selected_band="reject", display_section=None)))
        self.assertFalse(result["passed"])
        self.assertEqual(result["primary_retention"]["value"], 0)

    def test_primary_mislabel_fails(self):
        self.assertFalse(evaluate_quality(self.review(self.row(gold_band="reject")))["passed"])

    def test_unsupported_claim_fails(self):
        self.assertFalse(evaluate_quality(self.review(self.row(supported_material_claims=1)))["passed"])

    def test_empty_live_not_release_proof(self):
        result = evaluate_quality(self.review(self.row(gold_band="reject", selected_band="reject", display_section=None), kind="live"))
        self.assertEqual(result["status"], "VALID_EMPTY_READY_SAMPLE")
        self.assertFalse(result["adequate_sample_evidence"])

    def test_small_holdout_insufficient(self):
        result = evaluate_quality(self.review(self.row(), kind="holdout"))
        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")

    def test_unresolved_primary_never_counts_correct(self):
        result = evaluate_quality(self.review(self.row(gold_band="unresolved")))
        self.assertFalse(result["passed"])

    def test_valid_holdout(self):
        rows = [self.row(id=f"p{i}", display_section=("primary" if i < 5 else None), display_rank=(i + 1 if i < 5 else None)) for i in range(20)]
        rows += [self.row(id=f"r{i}", gold_band="reject", selected_band="reject", display_section=None) for i in range(80)]
        result = evaluate_quality(self.review(*rows, kind="holdout"))
        self.assertTrue(result["adequate_sample_evidence"])

    def test_verify_needs_concrete_task(self):
        row = self.row(gold_band="verify", selected_band="verify", display_section="verify", verify_useful=True, verification_task_complete=False)
        self.assertFalse(evaluate_quality(self.review(row))["passed"])

    def test_review_cannot_drop_a_corpus_id(self):
        review = self.review(self.row())
        review["corpus_record_ids"].append("dropped")
        self.assertFalse(evaluate_quality(review)["passed"])

    def test_rejected_gold_cannot_count_as_useful_verify(self):
        row = self.row(gold_band="reject", selected_band="verify", display_section="verify", verify_useful=True, verification_task_complete=True)
        self.assertFalse(evaluate_quality(self.review(row))["passed"])

    def test_rank_gap_fails(self):
        self.assertFalse(evaluate_quality(self.review(self.row(display_rank=2)))["passed"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    contracts = sub.add_parser("contracts")
    contracts.add_argument("--cases", type=Path, required=True)
    contracts.add_argument("--outputs", type=Path, required=True)
    contracts.add_argument("--report", type=Path)
    quality = sub.add_parser("quality")
    quality.add_argument("--review", type=Path, required=True)
    quality.add_argument("--report", type=Path)
    sub.add_parser("self-test")
    args = parser.parse_args(argv)
    if args.mode == "self-test":
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(EvaluatorTests))
        return 0 if result.wasSuccessful() else 1
    try:
        if args.mode == "contracts":
            result = evaluate_contracts(load_jsonl(args.cases), load_jsonl(args.outputs))
        else:
            value = load_json(args.review)
            if not isinstance(value, dict):
                raise ValueError("Review must be a JSON object")
            result = evaluate_quality(value)
        rendered = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        if args.report:
            if args.report.resolve() in {p.resolve() for p in (getattr(args, "cases", None), getattr(args, "outputs", None), getattr(args, "review", None)) if p is not None}:
                raise ValueError("Report must not overwrite an input file")
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        if not result["passed"]:
            return 1
        if args.mode == "quality" and not result["adequate_sample_evidence"]:
            return 2
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"Evaluation input error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
