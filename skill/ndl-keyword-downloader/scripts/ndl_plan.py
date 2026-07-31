#!/usr/bin/env python3
"""Build a reviewable NDL download plan from structured page inspections."""

from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ndl_common import WorkflowError, read_json, write_json_atomic


SCHEMA_VERSION = 1
ROLE_SUFFIXES = re.compile(
    r"(共著|編著|著作|編集|監修|校訂|訳注|解説|講演|述|著|編|訳|撰|選)+$"
)
UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def normalized(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


def sanitize_component(value: Any, limit: int = 100) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = UNSAFE_FILENAME.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    return (text or "untitled")[:limit].rstrip(" ._")


def field_matches(value: Any, keyword: str, mode: str) -> bool:
    if mode == "off":
        return False
    value_norm = normalized(value)
    keyword_norm = normalized(keyword)
    if not value_norm or not keyword_norm:
        return False
    if mode == "contains":
        return keyword_norm in value_norm
    if mode == "exact":
        return value_norm == keyword_norm
    if mode == "person":
        parts = re.split(r"[,、;；・/／]|(?:\s+ほか)", str(value or ""))
        cleaned = [normalized(ROLE_SUFFIXES.sub("", part.strip())) for part in parts]
        return keyword_norm in cleaned
    raise WorkflowError(f"Unknown match mode: {mode}")


def any_keyword_matches(value: Any, keywords: Sequence[str], mode: str = "contains") -> bool:
    return any(field_matches(value, keyword, mode) for keyword in keywords)


def integer(value: Any) -> Optional[int]:
    try:
        result = int(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


def item_total(item: Dict[str, Any]) -> Optional[int]:
    return integer(item.get("total_frames") or item.get("total"))


def toc_entries(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    seen = set()
    for raw in item.get("toc") or []:
        if not isinstance(raw, dict):
            continue
        frame = integer(raw.get("frame") or raw.get("start_frame"))
        text = str(raw.get("text") or raw.get("title") or "").strip()
        if not frame or not text:
            continue
        key = (frame, normalized(text))
        if key in seen:
            continue
        seen.add(key)
        result.append({"frame": frame, "text": text, "href": raw.get("href", "")})
    return sorted(result, key=lambda row: (row["frame"], row["text"]))


def split_range(start: int, end: int, maximum: int) -> Iterable[Tuple[int, int, int]]:
    sequence = 1
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + maximum - 1)
        yield sequence, cursor, chunk_end
        sequence += 1
        cursor = chunk_end + 1


def row_base(item: Dict[str, Any], pid: str) -> Dict[str, Any]:
    return {
        "pid": pid,
        "url": item.get("url") or f"https://dl.ndl.go.jp/pid/{pid}",
        "title": str(item.get("title") or "").strip(),
        "author": str(item.get("author") or "").strip(),
        "publisher": str(item.get("publisher") or "").strip(),
        "publication_date": str(
            item.get("publication_date") or item.get("pubdate") or ""
        ).strip(),
        "doi": str(item.get("doi") or "").strip(),
        "access_scope": str(item.get("access_scope") or "").strip(),
    }


def full_rows(
    item: Dict[str, Any],
    pid: str,
    total: int,
    label: str,
    maximum: int,
    reason: str,
) -> List[Dict[str, Any]]:
    base = row_base(item, pid)
    title = sanitize_component(base["title"])
    rows = []
    for sequence, start, end in split_range(1, total, maximum):
        rows.append(
            {
                **base,
                "download_type": "full",
                "reason": reason,
                "article_title": "",
                "sequence": sequence,
                "start": start,
                "end": end,
                "expected_pages": end - start + 1,
                "filename": (
                    f"{sanitize_component(label)}_{title}_{sequence:02d}_"
                    f"{start:03d}-{end:03d}.pdf"
                ),
                "status": "pending",
            }
        )
    return rows


def article_rows(
    item: Dict[str, Any],
    pid: str,
    label: str,
    maximum: int,
    extra_after: int,
    keywords: Sequence[str],
    trust_last_toc: bool,
    manual_ranges: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    total = item_total(item)
    entries = toc_entries(item)
    hits = []
    if manual_ranges is not None:
        for raw in manual_ranges:
            start = integer(raw.get("start"))
            end = integer(raw.get("end"))
            title = str(raw.get("label") or raw.get("title") or "manual range").strip()
            if not start or not end or end < start:
                raise WorkflowError(f"{pid}: invalid manual range {raw!r}")
            range_extra = int(raw.get("extra_after", extra_after))
            if range_extra < 0:
                raise WorkflowError(f"{pid}: manual extra_after cannot be negative")
            hits.append(
                {
                    "start": start,
                    "target_end": end,
                    "download_end": end + range_extra,
                    "title": title,
                    "review": False,
                    "reason": "manual_range",
                    "extra_after": range_extra,
                }
            )
    else:
        matching_entries = [
            entry for entry in entries if any_keyword_matches(entry["text"], keywords)
        ]
        for entry in entries:
            if entry in matching_entries:
                next_frame = next(
                    (candidate["frame"] for candidate in entries if candidate["frame"] > entry["frame"]),
                    None,
                )
                review_flags = []
                if next_frame:
                    target_end = next_frame - 1
                    download_end = target_end + extra_after
                    if next_frame - entry["frame"] <= 1:
                        review_flags.append("suspiciously_short_toc_gap")
                    if any(
                        other["frame"] != entry["frame"]
                        and abs(other["frame"] - entry["frame"]) <= 2
                        for other in matching_entries
                    ):
                        review_flags.append("adjacent_keyword_toc_entries")
                    review = bool(review_flags)
                    reason = "toc_keyword_hit"
                elif total:
                    target_end = total
                    download_end = total
                    review = not trust_last_toc
                    if review:
                        review_flags.append("no_following_toc_entry")
                    reason = "toc_keyword_hit_without_following_entry"
                else:
                    target_end = None
                    download_end = None
                    review = True
                    review_flags.append("no_resolved_end")
                    reason = "toc_keyword_hit_without_end"
                hits.append(
                    {
                        "start": entry["frame"],
                        "target_end": target_end,
                        "download_end": download_end,
                        "title": entry["text"],
                        "review": review,
                        "review_flags": review_flags,
                        "reason": reason,
                        "extra_after": extra_after,
                    }
                )

    rows: List[Dict[str, Any]] = []
    reviews: List[Dict[str, Any]] = []
    seen = set()
    base = row_base(item, pid)
    for hit in hits:
        key = (hit["start"], hit["target_end"], normalized(hit["title"]))
        if key in seen:
            continue
        seen.add(key)
        if not hit["download_end"]:
            reviews.append({**base, **hit})
            continue
        end = int(hit["download_end"])
        if total:
            end = min(end, total)
        if end < int(hit["start"]):
            reviews.append({**base, **hit, "error": "resolved end precedes start"})
            continue
        for sequence, start, chunk_end in split_range(int(hit["start"]), end, maximum):
            status = "needs_review" if hit["review"] else "pending"
            row = {
                **base,
                "download_type": "article",
                "reason": hit["reason"],
                "article_title": hit["title"],
                "sequence": sequence,
                "start": start,
                "end": chunk_end,
                "target_end": hit["target_end"],
                "extra_after": hit.get("extra_after", extra_after),
                "review_flags": hit.get("review_flags", []),
                "expected_pages": chunk_end - start + 1,
                "filename": (
                    f"{sanitize_component(label)}_{sanitize_component(hit['title'])}_"
                    f"{pid}_{sequence:02d}_{start:03d}-{chunk_end:03d}.pdf"
                ),
                "status": status,
            }
            rows.append(row)
            if status == "needs_review":
                reviews.append(row)
    return rows, reviews


def disambiguate_filenames(rows: List[Dict[str, Any]]) -> None:
    counts = Counter(row["filename"] for row in rows)
    used = set()
    for row in rows:
        filename = row["filename"]
        if counts[filename] > 1:
            marker = f"_pid{row['pid']}"
            filename = re.sub(
                r"(_\d{2}_\d{3}-\d{3}\.pdf)$",
                marker + r"\1",
                filename,
            )
        candidate = filename
        suffix = 2
        while candidate in used:
            candidate = filename[:-4] + f"_{suffix}.pdf"
            suffix += 1
        row["filename"] = candidate
        row["id"] = f"{row['pid']}:{row['start']}-{row['end']}:{row['sequence']}"
        used.add(candidate)


def load_overrides(path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    if not path:
        return {}
    value = read_json(path)
    if not isinstance(value, dict):
        raise WorkflowError("Overrides must be a JSON object keyed by PID")
    return value


def tsv_value(value: Any) -> str:
    return re.sub(r"[\t\r\n]+", " ", str(value or "")).strip()


def write_checklist(path: Path, rows: List[Dict[str, Any]]) -> None:
    fields = [
        "pid",
        "title",
        "author",
        "publisher",
        "publication_date",
        "download_type",
        "reason",
        "review_flags",
        "article_title",
        "sequence",
        "start",
        "end",
        "expected_pages",
        "filename",
        "status",
        "url",
        "doi",
        "access_scope",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(fields)
        for row in rows:
            values = []
            for field in fields:
                value = row.get(field, "")
                if isinstance(value, list):
                    value = ",".join(str(item) for item in value)
                values.append(tsv_value(value))
            writer.writerow(values)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspection", required=True, type=Path)
    parser.add_argument("--keyword", action="append", required=True)
    parser.add_argument("--label", help="Filename prefix; defaults to the first keyword")
    parser.add_argument("--plan", type=Path, default=Path("ndl-download-plan.json"))
    parser.add_argument("--checklist", type=Path, default=Path("ndl-download-checklist.tsv"))
    parser.add_argument("--review", type=Path, default=Path("ndl-needs-review.json"))
    parser.add_argument("--overrides", type=Path)
    parser.add_argument("--max-frames", type=int, default=50)
    parser.add_argument("--extra-after", type=int, default=1)
    parser.add_argument(
        "--title-match",
        choices=["off", "contains", "exact"],
        default="contains",
    )
    parser.add_argument(
        "--author-match",
        choices=["off", "contains", "exact", "person"],
        default="contains",
    )
    parser.add_argument("--trust-last-toc", action="store_true")
    args = parser.parse_args()

    if args.max_frames < 1 or args.max_frames > 50:
        raise WorkflowError("--max-frames must be between 1 and 50")
    if args.extra_after < 0:
        raise WorkflowError("--extra-after cannot be negative")

    inspections = read_json(args.inspection)
    if not isinstance(inspections, list):
        raise WorkflowError("Inspection must be a JSON array")
    overrides = load_overrides(args.overrides)
    label = args.label or args.keyword[0]
    rows: List[Dict[str, Any]] = []
    reviews: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    seen_pids = set()

    for item in inspections:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("pid") or "").strip()
        if not pid.isdigit():
            reviews.append({"pid": pid, "reason": "missing_or_invalid_pid", "item": item})
            continue
        if pid in seen_pids:
            reviews.append({"pid": pid, "reason": "duplicate_inspection"})
            continue
        seen_pids.add(pid)
        override = overrides.get(pid, {})
        if not isinstance(override, dict):
            raise WorkflowError(f"{pid}: override must be an object")
        action = override.get("action")
        if action not in (None, "exclude", "full", "ranges"):
            raise WorkflowError(f"{pid}: unknown override action {action!r}")
        if action == "exclude":
            skipped.append({**row_base(item, pid), "reason": "manual_exclusion"})
            continue

        total = item_total(item)
        title_hit = any_keyword_matches(item.get("title"), args.keyword, args.title_match)
        author_hit = any_keyword_matches(item.get("author"), args.keyword, args.author_match)
        if action == "full" or title_hit or author_hit:
            if not total:
                reviews.append({**row_base(item, pid), "reason": "full_match_without_total"})
                continue
            reason = (
                "manual_full"
                if action == "full"
                else "title_keyword_hit"
                if title_hit
                else "author_keyword_hit"
            )
            rows.extend(full_rows(item, pid, total, label, args.max_frames, reason))
            continue

        manual_ranges = override.get("ranges") if action == "ranges" else None
        if action == "ranges" and not isinstance(manual_ranges, list):
            raise WorkflowError(f"{pid}: ranges override must contain a ranges array")
        article, article_reviews = article_rows(
            item,
            pid,
            label,
            args.max_frames,
            args.extra_after,
            args.keyword,
            args.trust_last_toc,
            manual_ranges,
        )
        if article:
            rows.extend(article)
            reviews.extend(article_reviews)
        else:
            skipped.append({**row_base(item, pid), "reason": "no_structured_keyword_hit"})

    disambiguate_filenames(rows)
    plan = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "keywords": args.keyword,
        "label": label,
        "rules": {
            "title_match": args.title_match,
            "author_match": args.author_match,
            "max_frames": args.max_frames,
            "extra_after": args.extra_after,
            "trust_last_toc": args.trust_last_toc,
        },
        "rows": rows,
        "skipped": skipped,
    }
    write_json_atomic(args.plan, plan)
    write_json_atomic(args.review, reviews)
    write_checklist(args.checklist, rows)
    counts = Counter(row["status"] for row in rows)
    print(
        json.dumps(
            {
                "plan": str(args.plan),
                "rows": len(rows),
                "status": dict(counts),
                "review_items": len(reviews),
                "skipped_items": len(skipped),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
