#!/usr/bin/env python3
"""Verify every planned NDL PDF against its expected frame count."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from ndl_common import (
    WorkflowError,
    pdf_page_count,
    plan_rows,
    read_json,
    safe_output_path,
    sha256_file,
    write_json_atomic,
)


def audit(args: argparse.Namespace) -> Dict[str, Any]:
    plan = read_json(args.plan)
    rows = plan_rows(plan)
    filenames = [str(row.get("filename") or "") for row in rows]
    duplicates = sorted(name for name, count in Counter(filenames).items() if count > 1)
    files: List[Dict[str, Any]] = []
    problems: List[Dict[str, Any]] = []

    for row in rows:
        filename = str(row.get("filename") or "")
        expected = int(
            row.get("expected_pages")
            or (int(row.get("end")) - int(row.get("start")) + 1)
        )
        path = safe_output_path(args.output_dir, filename)
        record: Dict[str, Any] = {
            "id": row.get("id"),
            "pid": row.get("pid"),
            "filename": filename,
            "expected_pages": expected,
        }
        if not path.exists():
            record["status"] = "missing"
            problems.append(record.copy())
        elif not path.is_file() or path.stat().st_size == 0:
            record["status"] = "empty_or_not_file"
            problems.append(record.copy())
        else:
            try:
                pages = pdf_page_count(path)
                record.update(
                    {
                        "actual_pages": pages,
                        "size_bytes": path.stat().st_size,
                        "status": "verified" if pages == expected else "page_count_mismatch",
                    }
                )
                if args.hash:
                    record["sha256"] = sha256_file(path)
                if pages != expected:
                    problems.append(record.copy())
            except WorkflowError as exc:
                record.update({"status": "unreadable", "error": str(exc)})
                problems.append(record.copy())
        files.append(record)

    planned = set(filenames)
    extras = []
    if args.find_extras:
        for path in sorted(args.output_dir.glob("*.pdf")):
            if path.name not in planned:
                extras.append({"filename": path.name, "size_bytes": path.stat().st_size})

    if duplicates:
        problems.append({"status": "duplicate_plan_filenames", "filenames": duplicates})

    report = {
        "schema_version": 1,
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "plan": str(args.plan),
        "output_dir": str(args.output_dir),
        "summary": {
            "plan_rows": len(rows),
            "verified": sum(record["status"] == "verified" for record in files),
            "problems": len(problems),
            "extra_pdfs": len(extras),
            "expected_pages": sum(record["expected_pages"] for record in files),
            "verified_pages": sum(
                record.get("actual_pages", 0)
                for record in files
                if record["status"] == "verified"
            ),
        },
        "files": files,
        "problems": problems,
        "extra_pdfs": extras,
    }
    if args.update_plan:
        by_filename = {record["filename"]: record for record in files}
        for row in rows:
            record = by_filename[row["filename"]]
            if record["status"] == "verified":
                row["status"] = "downloaded"
                row["actual_pages"] = record["actual_pages"]
                row.pop("error", None)
            elif row.get("status") == "downloaded":
                row["status"] = record["status"]
                row["error"] = record.get("error") or record["status"]
        write_json_atomic(args.plan, plan)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--report", type=Path, default=Path("ndl-audit.json"))
    parser.add_argument("--hash", action="store_true")
    parser.add_argument("--find-extras", action="store_true")
    parser.add_argument("--update-plan", action="store_true")
    args = parser.parse_args()
    report = audit(args)
    write_json_atomic(args.report, report)
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 1 if report["summary"]["problems"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
