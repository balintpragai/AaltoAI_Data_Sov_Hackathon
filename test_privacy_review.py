#!/usr/bin/env python3
"""Synthetic-only tests for redacted profiles and advisory privacy reviews."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from privacy_review import (
    ModelReview,
    assemble_review,
    assert_allowed_base_url,
    parse_model_review,
    require_role_for_column,
    shareable,
)
from redacted_profile import assert_no_forbidden_payload, build_redacted_profile


def synthetic_frame() -> pd.DataFrame:
    """Clearly fake rows. Unique identifier strings are only used as leak canaries."""
    n = 30
    rare = ["SYNTH_RARE_APP"] + ["web"] * (n - 1)
    return pd.DataFrame(
        {
            "msisdn": [f"SYNTHMSISDN{i:04d}" for i in range(n)],
            "imsi": [f"SYNTHIMSI{i:04d}" for i in range(n)],
            "imei": [f"{i:015d}" for i in range(n)],
            "enb": [9001] * 15 + [9002] * 10 + [9003] * 4 + [9004],
            "province": ["SYNTH_NORTH"] * 20 + ["SYNTH_SOUTH"] * 10,
            "radio_access_type": ["LTE"] * n,
            "application_category": rare,
            "tp_dl_avg": [float(i) + 0.123456 for i in range(n)],
            "mystery_cat": [f"LABEL_{i}" for i in range(n)],
        }
    )


def offline_model_json(profile: dict) -> dict:
    columns = []
    for col in profile["columns"]:
        if col["kind"] == "identifier_like":
            cls = "direct identifier"
            checks = [{"tests_py_id": "T1"}]
        elif col["kind"] == "enb" or col["name"] in {
            "province",
            "radio_access_type",
            "application_category",
        }:
            cls = "quasi-identifier"
            checks = [{"tests_py_id": "T3"}]
        elif col["kind"] == "numeric":
            cls = "kpi"
            checks = [{"tests_py_id": "T7"}]
        else:
            cls = "unknown"
            checks = [{"tests_py_id": None, "status": "not_implemented", "note": "no test yet"}]
        columns.append(
            {
                "name": col["name"],
                "proposed_class": cls,
                "rationale": "synthetic fixture citing WP216",
                "checks": checks,
            }
        )
    return {
        "columns": columns,
        "open_questions": [{"text": "Is the lawful basis documented?", "must_answer_proposed": True}],
    }


class RedactedProfileTests(unittest.TestCase):
    def test_does_not_leak_identifiers_or_enb_or_rare_labels(self) -> None:
        df = synthetic_frame()
        profile = build_redacted_profile(df, "raw", k_min=10)
        canaries = [
            "SYNTHMSISDN0000",
            "SYNTHIMSI0000",
            "SYNTH_RARE_APP",
            "LABEL_0",
            "9003",
            "9004",
        ]
        blob = json.dumps(profile)
        for c in canaries:
            self.assertNotIn(c, blob)
        assert_no_forbidden_payload(profile, canaries)
        self.assertNotIn("top_value", blob)

        by_name = {c["name"]: c for c in profile["columns"]}
        self.assertEqual(by_name["msisdn"]["kind"], "identifier_like")
        self.assertEqual(by_name["enb"]["kind"], "enb")
        self.assertIn("cell_size_histogram", by_name["enb"])
        self.assertNotIn("categories", by_name["enb"])
        cats = {row["label"] for row in by_name["application_category"]["categories"]}
        self.assertIn("web", cats)
        self.assertNotIn("SYNTH_RARE_APP", cats)
        self.assertEqual(by_name["mystery_cat"]["kind"], "categorical_nolabels")
        self.assertIn("p50", by_name["tp_dl_avg"])
        self.assertNotIn("min", by_name["tp_dl_avg"])
        self.assertIn("min_rounded", by_name["tp_dl_avg"])


class ReviewAssemblyTests(unittest.TestCase):
    def test_fail_closed_on_non_json(self) -> None:
        with self.assertRaises(SystemExit):
            parse_model_review("not json")

    def test_fail_closed_on_unknown_test_id(self) -> None:
        with self.assertRaises(SystemExit):
            parse_model_review(
                json.dumps(
                    {
                        "columns": [
                            {
                                "name": "x",
                                "proposed_class": "kpi",
                                "rationale": "x",
                                "checks": [{"tests_py_id": "T99"}],
                            }
                        ],
                        "open_questions": [],
                    }
                )
            )

    def test_assemble_and_roles_and_share_gate(self) -> None:
        profile = build_redacted_profile(synthetic_frame(), "release_candidate", k_min=10)
        draft = ModelReview.model_validate(offline_model_json(profile))
        review = assemble_review(profile, draft, "offline", "offline")
        self.assertTrue(review["shareable_release_candidate"])

        enb = next(c for c in review["columns"] if c["name"] == "enb")
        with self.assertRaises(SystemExit):
            require_role_for_column(enb, "engineer")
        require_role_for_column(enb, "data_owner")

        kpi = next(c for c in review["columns"] if c["name"] == "tp_dl_avg")
        require_role_for_column(kpi, "engineer")

        q = review["open_questions"][0]
        q["must_answer"] = True
        self.assertFalse(shareable(review))
        q["answered"] = True
        self.assertTrue(shareable(review))

        raw_profile = build_redacted_profile(synthetic_frame(), "raw", k_min=10)
        raw_review = assemble_review(
            raw_profile, ModelReview.model_validate(offline_model_json(raw_profile)), "offline", "offline"
        )
        self.assertFalse(shareable(raw_review))

    def test_column_mismatch_fails_closed(self) -> None:
        profile = build_redacted_profile(synthetic_frame(), "raw", k_min=10)
        payload = offline_model_json(profile)
        payload["columns"].pop()
        with self.assertRaises(SystemExit):
            assemble_review(profile, ModelReview.model_validate(payload), "offline", "offline")

    def test_openai_host_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            assert_allowed_base_url("https://api.openai.com/v1")
        with self.assertRaises(SystemExit):
            assert_allowed_base_url("https://foo.openai.com/v1")

    def test_offline_request_roundtrip(self) -> None:
        profile = build_redacted_profile(synthetic_frame(), "release_candidate", k_min=10)
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            profile_path = t / "profile.json"
            model_path = t / "model.json"
            review_path = t / "review.json"
            profile_path.write_text(json.dumps(profile))
            model_path.write_text(json.dumps(offline_model_json(profile)))
            from privacy_review import cmd_request

            ns = type(
                "A",
                (),
                {
                    "profile": str(profile_path),
                    "offline_json": str(model_path),
                    "output": str(review_path),
                },
            )()
            cmd_request(ns)
            review = json.loads(review_path.read_text())
            self.assertEqual(review["columns"][0]["disposition"], "pending")
            self.assertTrue(review_path.with_suffix(".md").exists())


if __name__ == "__main__":
    unittest.main()
