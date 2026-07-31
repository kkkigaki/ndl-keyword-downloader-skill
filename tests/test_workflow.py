import argparse
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skill" / "ndl-keyword-downloader" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ndl_archive
import ndl_audit
import ndl_plan
from ndl_common import WorkflowError, safe_output_path
from ndl_macos_chrome import build_parser, download, pending_rows, urls_from_file


def item(pid, title="", author="", total=120, toc=None, body_excerpt=""):
    return {
        "pid": str(pid),
        "url": f"https://dl.ndl.go.jp/pid/{pid}",
        "title": title,
        "author": author,
        "publisher": "Test Press",
        "publication_date": "1936",
        "volume_issue": "第1巻第2号",
        "call_number": "TEST-1",
        "bibliographic_id": "TEST-BIB-1",
        "reproduction_note": "転載時の表記例\n国立国会図書館デジタルコレクション",
        "total_frames": total,
        "toc": toc or [],
        "body_excerpt": body_excerpt,
    }


class PlanRulesTest(unittest.TestCase):
    def test_full_item_is_split_at_fifty_frames(self):
        rows = ndl_plan.full_rows(
            item("100", title="XXXXの本"),
            "100",
            121,
            "XXXX",
            50,
            "title_keyword_hit",
        )
        self.assertEqual([(r["start"], r["end"]) for r in rows], [(1, 50), (51, 100), (101, 121)])
        self.assertEqual([r["expected_pages"] for r in rows], [50, 50, 21])
        self.assertTrue(all(row["archive_key"] == "100:full" for row in rows))
        self.assertEqual(rows[0]["call_number"], "TEST-1")
        self.assertEqual(rows[0]["bibliographic_id"], "TEST-BIB-1")
        self.assertIn("転載時の表記例", rows[0]["reproduction_note"])

    def test_person_author_match_strips_role_suffix(self):
        self.assertTrue(ndl_plan.field_matches("XXXX 著", "XXXX", "person"))
        self.assertFalse(ndl_plan.field_matches("XXXX資料刊行会 編", "XXXX", "person"))

    def test_toc_article_includes_one_safety_frame(self):
        source = item(
            "101",
            title="雑誌",
            toc=[
                {"text": "XXXX　海軍論", "frame": 10},
                {"text": "次の記事", "frame": 20},
            ],
        )
        rows, reviews = ndl_plan.article_rows(
            source,
            "101",
            "XXXX",
            50,
            1,
            ["XXXX"],
            False,
        )
        self.assertFalse(reviews)
        self.assertEqual((rows[0]["start"], rows[0]["target_end"], rows[0]["end"]), (10, 19, 20))
        self.assertEqual(rows[0]["expected_pages"], 11)
        self.assertEqual(rows[0]["archive_key"], "101:article:10-19")

    def test_last_toc_hit_requires_review(self):
        source = item(
            "102",
            title="雑誌",
            total=80,
            toc=[{"text": "XXXX　最終記事", "frame": 70}],
        )
        rows, reviews = ndl_plan.article_rows(
            source,
            "102",
            "XXXX",
            50,
            1,
            ["XXXX"],
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
                {"text": "XXXX　論説", "frame": 15},
                {"text": "写真", "frame": 16},
                {"text": "次の記事", "frame": 25},
            ],
        )
        rows, reviews = ndl_plan.article_rows(
            source,
            "104",
            "XXXX",
            50,
            1,
            ["XXXX"],
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
                {"text": "XXXX　論説（上）", "frame": 20},
                {"text": "XXXX　論説（下）", "frame": 22},
                {"text": "次の記事", "frame": 30},
            ],
        )
        rows, reviews = ndl_plan.article_rows(
            source,
            "105",
            "XXXX",
            50,
            1,
            ["XXXX"],
            False,
        )
        self.assertTrue(all(row["status"] == "needs_review" for row in rows))
        self.assertEqual(len(reviews), 2)

    def test_body_text_is_not_a_structured_match(self):
        source = item(
            "103",
            title="無関係",
            author="対象外著者",
            body_excerpt="関連の資料\nXXXX",
        )
        self.assertFalse(ndl_plan.any_keyword_matches(source["title"], ["XXXX"]))
        self.assertFalse(ndl_plan.any_keyword_matches(source["author"], ["XXXX"]))
        self.assertEqual(ndl_plan.toc_entries(source), [])

    def test_duplicate_full_filenames_gain_pid(self):
        rows = []
        rows.extend(ndl_plan.full_rows(item("201", title="同名"), "201", 10, "XXXX", 50, "title"))
        rows.extend(ndl_plan.full_rows(item("202", title="同名"), "202", 10, "XXXX", 50, "title"))
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

    def test_public_examples_do_not_contain_known_person_names(self):
        forbidden = [
            "\u5e73\u7530\u664b\u7b56",
            "\u677e\u5ca1\u6d0b\u53f3",
        ]
        paths = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "skill").rglob("*"))]
        for path in paths:
            if not path.is_file() or path.suffix not in {".md", ".yaml", ".py"}:
                continue
            text = path.read_text(encoding="utf-8")
            for name in forbidden:
                self.assertNotIn(name, text, str(path))


class ArchiveTest(unittest.TestCase):
    def archive_plan(self, output):
        rows = ndl_plan.full_rows(
            item("301", title="XXXX研究", author="XXXX 著"),
            "301",
            70,
            "XXXX",
            50,
            "author_keyword_hit",
        )
        for row in rows:
            row["status"] = "downloaded"
            row["actual_pages"] = row["expected_pages"]
            row["downloaded_at"] = "2026-07-31T00:00:00+00:00"
            (output / row["filename"]).write_bytes(b"%PDF-test")
        article_rows, _ = ndl_plan.article_rows(
            item(
                "302",
                title="研究雑誌",
                author="編集部",
                toc=[
                    {"text": "XXXX　海軍論", "frame": 10},
                    {"text": "次の記事", "frame": 20},
                ],
            ),
            "302",
            "XXXX",
            50,
            1,
            ["XXXX"],
            False,
        )
        for row in article_rows:
            row["status"] = "downloaded"
            row["actual_pages"] = row["expected_pages"]
            row["downloaded_at"] = "2026-07-31T00:00:01+00:00"
            (output / row["filename"]).write_bytes(b"%PDF-test")
        return {"schema_version": 2, "rows": rows + article_rows}

    def test_archive_groups_chunks_and_preserves_article_citation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            records, problems = ndl_archive.build_archive_records(
                self.archive_plan(output),
                output,
            )
            self.assertFalse(problems)
            self.assertEqual(len(records), 2)
            whole = next(record for record in records if record["source_type"] == "整本")
            article = next(record for record in records if record["source_type"] == "期刊／单篇")
            self.assertEqual(whole["verified_pages"], 70)
            self.assertEqual(len(whole["filenames"].splitlines()), 2)
            self.assertEqual(article["target_frames"], "10-19")
            self.assertEqual(article["download_frames"], "10-20")
            self.assertIn("XXXX 海軍論", article["formatted_reference"])
            self.assertIn("NDLコマ10-19", article["formatted_reference"])
            self.assertIn("転載時の表記例", article["reproduction_note"])

    def test_download_parser_enables_archive_by_default(self):
        args = build_parser().parse_args(
            [
                "download",
                "--plan",
                "plan.json",
                "--output-dir",
                "pdfs",
                "--execute",
            ]
        )
        self.assertFalse(args.no_archive)
        self.assertIsNone(args.archive)

    def test_xlsx_archive_is_valid_ooxml_with_expected_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            records, problems = ndl_archive.build_archive_records(
                self.archive_plan(output),
                output,
            )
            self.assertFalse(problems)
            archive = output / "archive.xlsx"
            ndl_archive.write_xlsx(archive, records)
            self.assertTrue(zipfile.is_zipfile(archive))
            with zipfile.ZipFile(archive) as workbook:
                self.assertIn("xl/worksheets/sheet1.xml", workbook.namelist())
                sheet = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
                text = "".join(sheet.itertext())
            self.assertIn("転載時の表記例", text)
            self.assertIn("参考文献案", text)
            self.assertIn("XXXX研究", text)

    def test_partial_multi_chunk_download_is_marked_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            plan = self.archive_plan(output)
            full_rows = [
                row for row in plan["rows"] if row.get("download_type") == "full"
            ]
            full_rows[-1]["status"] = "pending"
            (output / full_rows[-1]["filename"]).unlink()
            records, problems = ndl_archive.build_archive_records(plan, output)
            self.assertFalse(problems)
            whole = next(record for record in records if record["source_type"] == "整本")
            self.assertEqual(whole["target_frames"], "1-70")
            self.assertEqual(whole["download_frames"], "1-50")
            self.assertEqual(whole["verified_pages"], 50)
            self.assertEqual(whole["file_status"], "部分完成 1/2")

    def test_download_refreshes_archive_for_existing_verified_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "pdfs"
            output.mkdir()
            pdf = output / "XXXX_test.pdf"
            pdf.write_bytes(b"%PDF-test")
            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "id": "1:1-1:1",
                                "archive_key": "1:full",
                                "pid": "1",
                                "download_type": "full",
                                "start": 1,
                                "end": 1,
                                "expected_pages": 1,
                                "filename": pdf.name,
                                "status": "pending",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                plan=plan_path,
                output_dir=output,
                pid=None,
                limit=None,
                all=True,
                include_review=False,
                replace_invalid=False,
                continue_on_error=False,
                delay=0,
                timeout=1,
                archive=root / "references.xlsx",
                no_archive=False,
                execute=True,
            )
            archive_result = {
                "archive": str(args.archive),
                "records": 1,
                "problems": [],
            }
            with mock.patch("ndl_macos_chrome.require_macos"), mock.patch(
                "ndl_macos_chrome.pdf_page_count",
                return_value=1,
            ), mock.patch(
                "ndl_macos_chrome.write_reference_archive",
                return_value=archive_result,
            ) as archive_writer:
                self.assertEqual(download(args), 0)
            archive_writer.assert_called_once()
            updated = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["rows"][0]["status"], "downloaded")
            self.assertEqual(updated["rows"][0]["actual_pages"], 1)

    def test_archive_failure_does_not_undo_verified_pdf_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "pdfs"
            output.mkdir()
            pdf = output / "XXXX_test.pdf"
            pdf.write_bytes(b"%PDF-test")
            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "id": "1:1-1:1",
                                "archive_key": "1:full",
                                "pid": "1",
                                "download_type": "full",
                                "start": 1,
                                "end": 1,
                                "expected_pages": 1,
                                "filename": pdf.name,
                                "status": "pending",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                plan=plan_path,
                output_dir=output,
                pid=None,
                limit=None,
                all=True,
                include_review=False,
                replace_invalid=False,
                continue_on_error=False,
                delay=0,
                timeout=1,
                archive=root / "references.xlsx",
                no_archive=False,
                execute=True,
            )
            with mock.patch("ndl_macos_chrome.require_macos"), mock.patch(
                "ndl_macos_chrome.pdf_page_count",
                return_value=1,
            ), mock.patch(
                "ndl_macos_chrome.write_reference_archive",
                side_effect=WorkflowError("archive failed"),
            ):
                with self.assertRaisesRegex(WorkflowError, "archive failed"):
                    download(args)
            updated = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["rows"][0]["status"], "downloaded")
            self.assertEqual(updated["rows"][0]["actual_pages"], 1)


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
