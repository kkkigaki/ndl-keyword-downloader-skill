import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skill" / "ndl-keyword-downloader" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ndl_audit
import ndl_plan
from ndl_common import WorkflowError, safe_output_path
from ndl_macos_chrome import pending_rows, urls_from_file


def item(pid, title="", author="", total=120, toc=None, body_excerpt=""):
    return {
        "pid": str(pid),
        "url": f"https://dl.ndl.go.jp/pid/{pid}",
        "title": title,
        "author": author,
        "publisher": "Test Press",
        "publication_date": "1936",
        "total_frames": total,
        "toc": toc or [],
        "body_excerpt": body_excerpt,
    }


class PlanRulesTest(unittest.TestCase):
    def test_full_item_is_split_at_fifty_frames(self):
        rows = ndl_plan.full_rows(
            item("100", title="平田晋策の本"),
            "100",
            121,
            "平田晋策",
            50,
            "title_keyword_hit",
        )
        self.assertEqual([(r["start"], r["end"]) for r in rows], [(1, 50), (51, 100), (101, 121)])
        self.assertEqual([r["expected_pages"] for r in rows], [50, 50, 21])

    def test_person_author_match_strips_role_suffix(self):
        self.assertTrue(ndl_plan.field_matches("平田晋策 著", "平田晋策", "person"))
        self.assertFalse(ndl_plan.field_matches("平田晋策伝記刊行会 編", "平田晋策", "person"))

    def test_toc_article_includes_one_safety_frame(self):
        source = item(
            "101",
            title="雑誌",
            toc=[
                {"text": "平田晋策　海軍論", "frame": 10},
                {"text": "次の記事", "frame": 20},
            ],
        )
        rows, reviews = ndl_plan.article_rows(
            source,
            "101",
            "平田晋策",
            50,
            1,
            ["平田晋策"],
            False,
        )
        self.assertFalse(reviews)
        self.assertEqual((rows[0]["start"], rows[0]["target_end"], rows[0]["end"]), (10, 19, 20))
        self.assertEqual(rows[0]["expected_pages"], 11)

    def test_last_toc_hit_requires_review(self):
        source = item(
            "102",
            title="雑誌",
            total=80,
            toc=[{"text": "平田晋策　最終記事", "frame": 70}],
        )
        rows, reviews = ndl_plan.article_rows(
            source,
            "102",
            "平田晋策",
            50,
            1,
            ["平田晋策"],
            False,
        )
        self.assertEqual(rows[0]["status"], "needs_review")
        self.assertEqual(rows[0]["end"], 80)
        self.assertEqual(len(reviews), 1)

    def test_one_frame_toc_gap_requires_review(self):
        source = item(
            "104",
            title="雑誌",
            toc=[
                {"text": "平田晋策　論説", "frame": 15},
                {"text": "写真", "frame": 16},
                {"text": "次の記事", "frame": 25},
            ],
        )
        rows, reviews = ndl_plan.article_rows(
            source,
            "104",
            "平田晋策",
            50,
            1,
            ["平田晋策"],
            False,
        )
        self.assertEqual(rows[0]["status"], "needs_review")
        self.assertIn("suspiciously_short_toc_gap", rows[0]["review_flags"])
        self.assertEqual(len(reviews), 1)

    def test_adjacent_keyword_entries_require_review(self):
        source = item(
            "105",
            title="雑誌",
            toc=[
                {"text": "平田晋策　論説（上）", "frame": 20},
                {"text": "平田晋策　論説（下）", "frame": 22},
                {"text": "次の記事", "frame": 30},
            ],
        )
        rows, reviews = ndl_plan.article_rows(
            source,
            "105",
            "平田晋策",
            50,
            1,
            ["平田晋策"],
            False,
        )
        self.assertTrue(all(row["status"] == "needs_review" for row in rows))
        self.assertEqual(len(reviews), 2)

    def test_body_text_is_not_a_structured_match(self):
        source = item(
            "103",
            title="無関係",
            author="別人 著",
            body_excerpt="関連の資料\n平田晋策",
        )
        self.assertFalse(ndl_plan.any_keyword_matches(source["title"], ["平田晋策"]))
        self.assertFalse(ndl_plan.any_keyword_matches(source["author"], ["平田晋策"]))
        self.assertEqual(ndl_plan.toc_entries(source), [])

    def test_duplicate_full_filenames_gain_pid(self):
        rows = []
        rows.extend(ndl_plan.full_rows(item("201", title="同名"), "201", 10, "人物", 50, "title"))
        rows.extend(ndl_plan.full_rows(item("202", title="同名"), "202", 10, "人物", 50, "title"))
        ndl_plan.disambiguate_filenames(rows)
        self.assertEqual(len({row["filename"] for row in rows}), 2)
        self.assertIn("pid201", rows[0]["filename"])
        self.assertIn("pid202", rows[1]["filename"])


class SafetyTest(unittest.TestCase):
    def test_output_filename_cannot_escape_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(safe_output_path(root, "safe.pdf"), (root / "safe.pdf").resolve())
            with self.assertRaises(WorkflowError):
                safe_output_path(root, "../escape.pdf")

    def test_review_rows_are_not_selected_by_default(self):
        rows = [
            {"pid": "1", "status": "pending"},
            {"pid": "1", "status": "needs_review"},
        ]
        self.assertEqual(len(pending_rows(rows, None, None, True, False)), 1)
        self.assertEqual(len(pending_rows(rows, None, None, True, True)), 2)

    def test_candidate_json_can_feed_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            path.write_text(
                json.dumps(
                    {
                        "results": [
                            {"pid": "10", "url": "https://dl.ndl.go.jp/pid/10"},
                            {"pid": "11", "url": "https://dl.ndl.go.jp/pid/11"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                urls_from_file(path),
                ["https://dl.ndl.go.jp/pid/10", "https://dl.ndl.go.jp/pid/11"],
            )


class AuditTest(unittest.TestCase):
    def test_audit_reports_missing_and_verified_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "pdfs"
            output.mkdir()
            (output / "present.pdf").write_bytes(b"%PDF-test")
            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "id": "1:1-2:1",
                                "pid": "1",
                                "start": 1,
                                "end": 2,
                                "expected_pages": 2,
                                "filename": "present.pdf",
                                "status": "pending",
                            },
                            {
                                "id": "2:1-1:1",
                                "pid": "2",
                                "start": 1,
                                "end": 1,
                                "expected_pages": 1,
                                "filename": "missing.pdf",
                                "status": "pending",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                plan=plan_path,
                output_dir=output,
                hash=False,
                find_extras=False,
                update_plan=False,
            )
            with mock.patch.object(ndl_audit, "pdf_page_count", return_value=2):
                report = ndl_audit.audit(args)
            self.assertEqual(report["summary"]["verified"], 1)
            self.assertEqual(report["summary"]["problems"], 1)
            self.assertEqual(report["problems"][0]["status"], "missing")


if __name__ == "__main__":
    unittest.main()
