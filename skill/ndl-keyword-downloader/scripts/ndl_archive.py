#!/usr/bin/env python3
"""Build an Excel reference archive from verified NDL download plan rows."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import time
import zipfile
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from ndl_common import WorkflowError, plan_rows, read_json, safe_output_path


SHEET_NAME = "参考文献归档"
ARCHIVE_FILENAME = "ndl-reference-archive.xlsx"
INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
EXCEL_CELL_LIMIT = 32767
COLUMNS: Sequence[Tuple[str, str, float]] = (
    ("archive_id", "归档编号", 24),
    ("source_type", "资料类型", 13),
    ("title", "书名／刊名", 30),
    ("article_title", "文章名", 34),
    ("author", "作者", 24),
    ("publisher", "出版者", 22),
    ("publication_date", "出版日期", 15),
    ("volume_issue", "卷期", 14),
    ("target_frames", "目标范围（NDL面）", 19),
    ("download_frames", "下载范围（含安全面）", 24),
    ("filenames", "下载文件", 42),
    ("verified_pages", "核验页数", 12),
    ("file_status", "文件核验状态", 16),
    ("pid", "PID", 14),
    ("url", "NDL链接", 38),
    ("doi", "DOI", 24),
    ("call_number", "请求记号", 18),
    ("bibliographic_id", "书志ID", 18),
    ("access_scope", "公开范围", 24),
    ("reproduction_note", "転載時の表記例", 48),
    ("formatted_reference", "参考文献案", 58),
    ("downloaded_at", "下载完成时间", 24),
)


def clean_cell(value: Any) -> str:
    text = INVALID_XML.sub("", str(value or ""))
    return text[:EXCEL_CELL_LIMIT]


def normalized_space(value: Any) -> str:
    return re.sub(r"\s+", " ", clean_cell(value)).strip()


def archive_key(row: Dict[str, Any]) -> str:
    explicit = normalized_space(row.get("archive_key"))
    if explicit:
        return explicit
    pid = normalized_space(row.get("pid")) or "unknown"
    if row.get("download_type") == "article":
        target_end = row.get("target_end") or row.get("end") or ""
        article_start = row.get("article_start")
        if article_start:
            return f"{pid}:article:{article_start}-{target_end}"
        return f"{pid}:article:{target_end}:{normalized_space(row.get('article_title'))}"
    return f"{pid}:full"


def inclusive_range(rows: Sequence[Dict[str, Any]], start_field: str, end_field: str) -> str:
    starts = [int(row[start_field]) for row in rows if row.get(start_field) not in (None, "")]
    ends = [int(row[end_field]) for row in rows if row.get(end_field) not in (None, "")]
    if not starts or not ends:
        return ""
    return f"{min(starts)}-{max(ends)}"


def access_date(row: Dict[str, Any]) -> str:
    for field in ("downloaded_at", "captured_at"):
        value = normalized_space(row.get(field))
        match = re.match(r"(\d{4}-\d{2}-\d{2})", value)
        if match:
            return match.group(1)
    return datetime.now(timezone.utc).date().isoformat()


def format_reference(row: Dict[str, Any], target_frames: str) -> str:
    author = normalized_space(row.get("author"))
    title = normalized_space(row.get("title"))
    article_title = normalized_space(row.get("article_title"))
    publisher = normalized_space(row.get("publisher"))
    publication_date = normalized_space(row.get("publication_date"))
    volume_issue = normalized_space(row.get("volume_issue"))
    url = normalized_space(row.get("url"))
    doi = normalized_space(row.get("doi"))

    opening = author
    if article_title:
        opening += (" " if opening else "") + f"「{article_title}」"
        if title:
            opening += f"『{title}』"
    elif title:
        opening += (" " if opening else "") + f"『{title}』"

    parts = [opening, volume_issue, publisher, publication_date]
    if article_title and target_frames:
        parts.append(f"NDLコマ{target_frames}")
    parts.append("国立国会図書館デジタルコレクション")
    if doi:
        parts.append(f"DOI: {doi}")
    if url:
        parts.append(url)
    citation = "，".join(part for part in parts if part)
    return f"{citation}（参照 {access_date(row)}）" if citation else ""


def build_archive_records(
    plan: Any,
    output_dir: Path,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    groups: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for row in plan_rows(plan):
        groups.setdefault(archive_key(row), []).append(row)

    records: List[Dict[str, Any]] = []
    problems: List[str] = []
    for key, rows in groups.items():
        rows.sort(key=lambda row: (int(row.get("start") or 0), int(row.get("sequence") or 0)))
        completed_rows = [row for row in rows if row.get("status") == "downloaded"]
        if not completed_rows:
            continue
        first = rows[0]
        filenames = []
        expected_total = 0
        actual_total = 0
        file_states = []
        for row in completed_rows:
            filename = normalized_space(row.get("filename"))
            if filename:
                filenames.append(filename)
            expected = int(row.get("expected_pages") or 0)
            expected_total += expected
            actual = int(row.get("actual_pages") or expected)
            actual_total += actual
            path = safe_output_path(output_dir, filename)
            if not path.exists():
                state = "文件缺失"
                problems.append(f"{key}: missing {filename}")
            elif actual != expected:
                state = "页数不符"
                problems.append(f"{key}: {filename} has {actual} pages; expected {expected}")
            else:
                state = "已验证"
            file_states.append(state)

        article = first.get("download_type") == "article"
        download_frames = inclusive_range(completed_rows, "start", "end")
        if article:
            target_start = int(first.get("article_start") or min(int(row["start"]) for row in rows))
            target_end = int(first.get("target_end") or max(int(row["end"]) for row in rows))
            target_frames = f"{target_start}-{target_end}"
            source_type = "期刊／单篇"
        else:
            target_frames = inclusive_range(rows, "start", "end")
            source_type = "整本"

        downloaded_times = sorted(
            normalized_space(row.get("downloaded_at"))
            for row in completed_rows
            if normalized_space(row.get("downloaded_at"))
        )
        if file_states and set(file_states) != {"已验证"}:
            file_status = "；".join(dict.fromkeys(file_states))
        elif len(completed_rows) < len(rows):
            file_status = f"部分完成 {len(completed_rows)}/{len(rows)}"
        else:
            file_status = "已验证"
        record = {
            "archive_id": key,
            "source_type": source_type,
            "title": normalized_space(first.get("title")),
            "article_title": normalized_space(first.get("article_title")),
            "author": normalized_space(first.get("author")),
            "publisher": normalized_space(first.get("publisher")),
            "publication_date": normalized_space(first.get("publication_date")),
            "volume_issue": normalized_space(first.get("volume_issue")),
            "target_frames": target_frames,
            "download_frames": download_frames,
            "filenames": "\n".join(filenames),
            "verified_pages": actual_total,
            "file_status": file_status,
            "pid": normalized_space(first.get("pid")),
            "url": normalized_space(first.get("url")),
            "doi": normalized_space(first.get("doi")),
            "call_number": normalized_space(first.get("call_number")),
            "bibliographic_id": normalized_space(first.get("bibliographic_id")),
            "access_scope": normalized_space(first.get("access_scope")),
            "reproduction_note": clean_cell(first.get("reproduction_note")).strip(),
            "formatted_reference": format_reference(first, target_frames),
            "downloaded_at": downloaded_times[-1] if downloaded_times else "",
        }
        if expected_total and actual_total != expected_total:
            problems.append(
                f"{key}: verified page total {actual_total} does not match expected {expected_total}"
            )
        records.append(record)
    return records, problems


def column_letter(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def xml_text(value: Any) -> str:
    return html.escape(clean_cell(value), quote=False)


def cell(ref: str, value: Any, style: int) -> str:
    if isinstance(value, int):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    return (
        f'<c r="{ref}" s="{style}" t="inlineStr"><is>'
        f'<t xml:space="preserve">{xml_text(value)}</t></is></c>'
    )


def worksheet_xml(records: Sequence[Dict[str, Any]]) -> str:
    last_column = column_letter(len(COLUMNS))
    last_row = max(2, len(records) + 2)
    widths = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, (_, _, width) in enumerate(COLUMNS, start=1)
    )
    note = (
        "自动归档。目标范围不含文章后的安全面；下载范围包含实际保存的安全面。"
        "“参考文献案”须按投稿期刊格式复核，“転載時の表記例”保留NDL页面原文。"
    )
    title_row = (
        '<row r="1" ht="36" customHeight="1">'
        + cell("A1", note, 3)
        + "</row>"
    )
    header_cells = "".join(
        cell(f"{column_letter(index)}2", label, 1)
        for index, (_, label, _) in enumerate(COLUMNS, start=1)
    )
    rows_xml = [title_row, f'<row r="2" ht="34" customHeight="1">{header_cells}</row>']
    for row_number, record in enumerate(records, start=3):
        cells = []
        for index, (key, _, _) in enumerate(COLUMNS, start=1):
            style = 5 if key == "verified_pages" else 4 if key == "url" else 2
            cells.append(cell(f"{column_letter(index)}{row_number}", record.get(key, ""), style))
        rows_xml.append(
            f'<row r="{row_number}" ht="66" customHeight="1">{"".join(cells)}</row>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last_column}{last_row}"/>'
        '<sheetViews><sheetView workbookViewId="0" showGridLines="0">'
        '<pane xSplit="2" ySplit="2" topLeftCell="C3" activePane="bottomRight" state="frozen"/>'
        "</sheetView></sheetViews>"
        '<sheetFormatPr defaultRowHeight="18"/>'
        f"<cols>{widths}</cols>"
        f'<sheetData>{"".join(rows_xml)}</sheetData>'
        f'<autoFilter ref="A2:{last_column}{last_row}"/>'
        f'<mergeCells count="1"><mergeCell ref="A1:{last_column}1"/></mergeCells>'
        '<pageMargins left="0.25" right="0.25" top="0.5" bottom="0.5" '
        'header="0.2" footer="0.2"/>'
        "</worksheet>"
    )


def styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="4">
    <font><sz val="11"/><name val="Calibri"/><family val="2"/></font>
    <font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font>
    <font><b/><color rgb="FFFFFFFF"/><sz val="12"/><name val="Calibri"/></font>
    <font><color rgb="FF0563C1"/><u/><sz val="11"/><name val="Calibri"/></font>
  </fonts>
  <fills count="4">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF0F6B5D"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border>
      <left style="thin"><color rgb="FFD9E2F3"/></left>
      <right style="thin"><color rgb="FFD9E2F3"/></right>
      <top style="thin"><color rgb="FFD9E2F3"/></top>
      <bottom style="thin"><color rgb="FFD9E2F3"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="6">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyAlignment="1">
      <alignment horizontal="center" vertical="center" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1">
      <alignment vertical="top" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyAlignment="1">
      <alignment vertical="center" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="3" fillId="0" borderId="1" xfId="0" applyAlignment="1">
      <alignment vertical="top" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1">
      <alignment horizontal="center" vertical="top"/>
    </xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def write_xlsx(path: Path, records: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""
    workbook = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <bookViews><workbookView/></bookViews>
  <sheets><sheet name="{xml_text(SHEET_NAME)}" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
    core = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>NDL参考文献归档</dc:title>
  <dc:creator>NDL Keyword Downloader</dc:creator>
  <dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified>
</cp:coreProperties>"""
    app = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>NDL Keyword Downloader</Application>
  <TitlesOfParts><vt:vector size="1" baseType="lpstr"><vt:lpstr>{xml_text(SHEET_NAME)}</vt:lpstr></vt:vector></TitlesOfParts>
</Properties>"""
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types)
            archive.writestr("_rels/.rels", root_rels)
            archive.writestr("docProps/core.xml", core)
            archive.writestr("docProps/app.xml", app)
            archive.writestr("xl/workbook.xml", workbook)
            archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
            archive.writestr("xl/styles.xml", styles_xml())
            archive.writestr("xl/worksheets/sheet1.xml", worksheet_xml(records))
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_reference_archive(
    plan: Any,
    output_dir: Path,
    archive_path: Path,
) -> Dict[str, Any]:
    records, problems = build_archive_records(plan, output_dir)
    write_xlsx(archive_path, records)
    return {
        "archive": str(archive_path),
        "records": len(records),
        "problems": problems,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()

    plan = read_json(args.plan)
    archive_path = args.archive or args.output_dir / ARCHIVE_FILENAME
    result = write_reference_archive(plan, args.output_dir, archive_path)
    print(json.dumps(result, ensure_ascii=False))
    if result["problems"]:
        raise WorkflowError(
            "Reference archive was written with verification problems: "
            + "; ".join(result["problems"])
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
