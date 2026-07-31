#!/usr/bin/env python3
"""Collect NDL metadata or execute a reviewed plan through macOS Chrome."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ndl_common import (
    WorkflowError,
    pdf_page_count,
    plan_rows,
    read_json,
    safe_output_path,
    write_json_atomic,
)


SCRIPT_DIR = Path(__file__).resolve().parent
APPLE_JS = SCRIPT_DIR / "chrome_execute_js.applescript"
PID_URL = re.compile(r"^https://dl\.ndl\.go\.jp/pid/(\d+)(?:/|$)")

INSPECTOR_JS = r"""
(() => {
  const body = document.body?.innerText || "";
  const pidMatch = location.pathname.match(/^\/pid\/(\d+)/);
  const pid = pidMatch ? pidMatch[1] : "";
  const detailStart = body.indexOf("\n永続的識別子\n");
  const detail = detailStart >= 0 ? body.slice(detailStart) : body;
  const field = (label) => {
    const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = detail.match(new RegExp("(?:^|\\n)" + escaped + "\\n([^\\n]+)"));
    return match ? match[1].trim() : "";
  };
  const totalMatch = body.match(/コマ番号\s*\n?\s*\/\s*(\d+)/);
  const accessMatch = detail.match(/(?:^|\n)公開範囲\n([^\n]+)/);
  const toc = [];
  const seen = new Set();
  for (const anchor of document.querySelectorAll("a[href]")) {
    let href = "";
    try { href = new URL(anchor.href, location.href).href; } catch (_) { continue; }
    const match = href.match(new RegExp("/pid/" + pid + "/1/(\\d+)(?:[/?#]|$)"));
    const text = (anchor.innerText || anchor.textContent || "").trim();
    if (!match || !text) continue;
    const key = match[1] + "|" + text.normalize("NFKC");
    if (seen.has(key)) continue;
    seen.add(key);
    toc.push({text, href, frame: Number(match[1])});
  }
  toc.sort((a, b) => a.frame - b.frame || a.text.localeCompare(b.text, "ja"));
  const citationTrigger = Array.from(document.querySelectorAll("button,a,summary"))
    .find((node) => (node.innerText || "").includes("転載時の表記例"));
  const citationContainer = citationTrigger?.closest("section,details,div,li");
  const fallbackTitle = document.title.replace(/\s*-\s*国立国会図書館デジタルコレクション\s*$/, "");
  return JSON.stringify({
    pid,
    url: "https://dl.ndl.go.jp/pid/" + pid,
    document_title: document.title,
    title: field("タイトル") || fallbackTitle,
    author: field("著者"),
    publisher: field("出版者"),
    publication_date: field("出版年月日"),
    call_number: field("請求記号"),
    bibliographic_id: field("書誌ID"),
    doi: field("識別子（DOI）"),
    access_scope: accessMatch ? accessMatch[1].trim() : "",
    total_frames: totalMatch ? Number(totalMatch[1]) : null,
    toc,
    reproduction_note: (citationContainer?.innerText || "").slice(0, 1200),
    captured_at: new Date().toISOString()
  });
})()
""".strip()

SEARCH_RESULTS_JS = r"""
(() => {
  const results = [];
  const seen = new Set();
  for (const anchor of document.querySelectorAll("a[href]")) {
    let url;
    try { url = new URL(anchor.href, location.href); } catch (_) { continue; }
    if (url.hostname !== "dl.ndl.go.jp") continue;
    const match = url.pathname.match(/^\/pid\/(\d+)(?:\/|$)/);
    if (!match || seen.has(match[1])) continue;
    seen.add(match[1]);
    results.push({
      pid: match[1],
      url: "https://dl.ndl.go.jp/pid/" + match[1],
      link_text: (anchor.innerText || anchor.textContent || "").trim(),
      source_search_url: location.href
    });
  }
  return JSON.stringify({
    source_search_url: location.href,
    page_title: document.title,
    captured_at: new Date().toISOString(),
    results
  });
})()
""".strip()


def require_macos() -> None:
    if sys.platform != "darwin":
        raise WorkflowError(
            "This adapter requires macOS. Use a browser-control tool and the inspector "
            "JavaScript on other platforms."
        )
    if not shutil.which("osascript"):
        raise WorkflowError("osascript is not available")


def run_osascript(arguments: List[str], timeout: int = 60) -> str:
    require_macos()
    result = subprocess.run(
        ["osascript", *arguments],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown AppleScript error"
        raise WorkflowError(message)
    return result.stdout.strip()


def run_js(code: str, timeout: int = 60) -> str:
    try:
        return run_osascript([str(APPLE_JS), code], timeout=timeout)
    except WorkflowError as exc:
        if "JavaScript" in str(exc) or "Apple Events" in str(exc):
            raise WorkflowError(
                "Chrome rejected JavaScript from Apple Events. In Chrome, enable "
                "View > Developer > Allow JavaScript from Apple Events."
            ) from exc
        raise


def chrome_urls() -> List[str]:
    script = """
tell application "Google Chrome"
  set output to ""
  repeat with w in windows
    repeat with t in tabs of w
      set output to output & (URL of t) & linefeed
    end repeat
  end repeat
  return output
end tell
"""
    return [line.strip() for line in run_osascript(["-e", script]).splitlines() if line.strip()]


def activate_pid(pid: str, open_missing: bool = True) -> None:
    if not pid.isdigit():
        raise WorkflowError(f"Invalid PID: {pid!r}")
    create = """
  set newTab to make new tab at end of front window with properties {URL:targetUrl}
  set active tab index of front window to (count tabs of front window)
  activate
  return URL of newTab
""" if open_missing else '  error "PID tab is not open"\n'
    script = f"""
tell application "Google Chrome"
  set targetUrl to "https://dl.ndl.go.jp/pid/{pid}"
  repeat with w in windows
    set tabIndex to 0
    repeat with t in tabs of w
      set tabIndex to tabIndex + 1
      if (URL of t) is targetUrl or (URL of t) starts with (targetUrl & "/") then
        set active tab index of w to tabIndex
        set index of w to 1
        activate
        return URL of t
      end if
    end repeat
  end repeat
{create}
end tell
"""
    run_osascript(["-e", script], timeout=30)


def wait_for_pid(pid: str, timeout: int = 90) -> None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            state = json.loads(
                run_js(
                    "JSON.stringify({url:location.href,ready:document.readyState,"
                    "text:(document.body?.innerText||'').slice(0,400)})",
                    timeout=15,
                )
            )
            if f"/pid/{pid}" in state.get("url", "") and state.get("ready") != "loading":
                return
        except (WorkflowError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        time.sleep(1)
    raise WorkflowError(f"PID {pid} did not become ready: {last_error}")


def unique_pids(urls: Iterable[str]) -> List[str]:
    result = []
    seen = set()
    for url in urls:
        match = PID_URL.match(url)
        if match and match.group(1) not in seen:
            seen.add(match.group(1))
            result.append(match.group(1))
    return result


def urls_from_file(path: Path) -> List[str]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return [line.strip() for line in text.splitlines() if line.strip()]
    if isinstance(value, dict):
        value = value.get("results")
    if not isinstance(value, list):
        raise WorkflowError(f"{path}: expected URL lines or a JSON results array")
    urls = []
    for item in value:
        if isinstance(item, str):
            urls.append(item)
        elif isinstance(item, dict) and item.get("url"):
            urls.append(str(item["url"]))
    return urls


def search_results(args: argparse.Namespace) -> int:
    payload = json.loads(run_js(SEARCH_RESULTS_JS, timeout=30))
    source_url = str(payload.get("source_search_url") or "")
    if not source_url.startswith("https://dl.ndl.go.jp/"):
        raise WorkflowError("The active tab is not an NDL page")
    existing: List[Dict[str, Any]] = []
    if args.append and args.output.exists():
        value = read_json(args.output)
        if isinstance(value, dict):
            existing = value.get("results") or []
        elif isinstance(value, list):
            existing = value
    combined = existing + list(payload.get("results") or [])
    deduplicated = []
    seen = set()
    for item in combined:
        pid = str(item.get("pid") or "") if isinstance(item, dict) else ""
        if pid.isdigit() and pid not in seen:
            seen.add(pid)
            deduplicated.append(item)
    output = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "last_search_url": source_url,
        "results": deduplicated,
    }
    write_json_atomic(args.output, output)
    print(
        json.dumps(
            {"output": str(args.output), "candidate_pids": len(deduplicated)},
            ensure_ascii=False,
        )
    )
    return 0


def collect(args: argparse.Namespace) -> int:
    urls = [] if args.url_file else chrome_urls()
    if args.url_file:
        urls.extend(urls_from_file(args.url_file))
    pids = unique_pids(urls)
    if args.pid:
        requested = set(args.pid)
        pids = [pid for pid in pids if pid in requested]
        missing = requested.difference(pids)
        if missing and args.open_missing:
            pids.extend(sorted(missing))
        elif missing:
            raise WorkflowError(
                "Requested PID tabs are not open: " + ", ".join(sorted(missing))
            )
    inspections: List[Dict[str, Any]] = []
    for pid in pids:
        activate_pid(pid, open_missing=args.open_missing)
        wait_for_pid(pid)
        try:
            item = json.loads(run_js(INSPECTOR_JS, timeout=30))
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"PID {pid} returned invalid inspection JSON") from exc
        if item.get("pid") != pid:
            raise WorkflowError(f"PID mismatch: expected {pid}, inspected {item.get('pid')}")
        inspections.append(item)
        write_json_atomic(args.output, inspections)
        print(json.dumps({"pid": pid, "status": "inspected"}, ensure_ascii=False), flush=True)
    print(
        json.dumps(
            {"output": str(args.output), "inspected": len(inspections)},
            ensure_ascii=False,
        )
    )
    return 0


def current_pdf_link() -> str:
    return run_js(
        "Array.from(document.querySelectorAll('a')).find("
        "a=>(a.innerText||'').includes('PDFファイルを開く'))?.href || ''",
        timeout=15,
    )


def page_mode() -> str:
    value = run_js(
        "JSON.stringify({"
        "panel:!!document.querySelector('#panel-download-download-button'),"
        "print:!!document.querySelector('#open-printing-modal')||"
        "!!document.querySelector('#open-internet-printing-modal')||"
        "Array.from(document.querySelectorAll('button')).some("
        "b=>(b.innerText||'').trim()==='印刷')"
        "})",
        timeout=15,
    )
    state = json.loads(value)
    if state.get("panel"):
        return "panel"
    if state.get("print"):
        return "print"
    raise WorkflowError("No supported NDL print/download control is visible")


def open_print_modal() -> None:
    result = run_js(
        "(()=>{"
        "document.querySelector('#printing-modal-cancel-button')?.click();"
        "const opener=document.querySelector('#open-printing-modal')||"
        "document.querySelector('#open-internet-printing-modal')||"
        "Array.from(document.querySelectorAll('button')).find("
        "b=>(b.innerText||'').trim()==='印刷');"
        "if(!opener)return 'missing';opener.click();return 'opened';"
        "})()",
        timeout=15,
    )
    if result != "opened":
        raise WorkflowError("Could not open the print dialog")
    deadline = time.time() + 30
    while time.time() < deadline:
        ready = run_js(
            "Boolean(document.querySelector('#printing-modal-range-specific')||"
            "document.querySelector('#range-specific'))+'|'+"
            "Boolean(document.querySelector('#range-specific-input'))",
            timeout=15,
        )
        if ready == "true|true":
            return
        time.sleep(0.5)
    raise WorkflowError("NDL print range controls did not appear")


def request_print_range(start: int, end: int) -> str:
    range_text = str(start) if start == end else f"{start}-{end}"
    literal = json.dumps(range_text)
    result = run_js(
        "(()=>{"
        "const radio=document.querySelector('#printing-modal-range-specific')||"
        "document.querySelector('#range-specific');"
        "const input=document.querySelector('#range-specific-input');"
        "if(!radio||!input)return JSON.stringify({ok:false,error:'missing-range-controls'});"
        "radio.click();radio.checked=true;"
        "radio.dispatchEvent(new Event('input',{bubbles:true}));"
        "radio.dispatchEvent(new Event('change',{bubbles:true}));"
        f"input.value={literal};"
        "input.dispatchEvent(new Event('input',{bubbles:true}));"
        "input.dispatchEvent(new Event('change',{bubbles:true}));"
        "const button=document.querySelector('#printing-modal-printing-button')||"
        "document.querySelector('#internet-printing-modal-printing-button')||"
        "Array.from(document.querySelectorAll('button')).find("
        "b=>/印刷用ファイル|ファイルを作成/.test(b.innerText||''));"
        "if(!button)return JSON.stringify({ok:false,error:'missing-create-button'});"
        "button.click();"
        "return JSON.stringify({ok:true,range:input.value,specific:radio.checked});"
        "})()",
        timeout=15,
    )
    state = json.loads(result)
    if not state.get("ok") or not state.get("specific") or state.get("range") != range_text:
        raise WorkflowError(f"NDL print range was not accepted: {state}")
    return range_text


def wait_for_generated_link(previous: str, timeout: int) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        link = current_pdf_link()
        if link and link != previous:
            return link
        time.sleep(2)
    raise WorkflowError(
        "NDL did not produce a new PDF link before timeout. Refusing to reuse a "
        "stale link because it may point to the wrong frame range."
    )


def fetch_generated_pdf(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            with destination.open("wb") as handle:
                shutil.copyfileobj(response, handle)
    except Exception as exc:
        raise WorkflowError(
            "Could not fetch the generated PDF URL. Keep the logged-in page open and "
            "retry; this adapter never reads Chrome cookies or stored credentials."
        ) from exc


def panel_download(start: int, end: int, destination: Path, timeout: int) -> None:
    downloads = Path.home() / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    before = {
        path.resolve(): (path.stat().st_mtime_ns, path.stat().st_size)
        for path in downloads.glob("*.pdf")
    }
    range_text = str(start) if start == end else f"{start}-{end}"
    literal = json.dumps(range_text)
    clicked_at = time.time()
    state = json.loads(
        run_js(
            "(()=>{"
            "const pdf=document.querySelector('#file-type-pdf');"
            "const radio=document.querySelector('#range-specific');"
            "const input=document.querySelector('#range-specific-input');"
            "const button=document.querySelector('#panel-download-download-button');"
            "if(!pdf||!radio||!input||!button)"
            "return JSON.stringify({ok:false,error:'missing-panel-control'});"
            "pdf.click();pdf.checked=true;"
            "radio.click();radio.checked=true;"
            f"input.value={literal};"
            "for(const node of [pdf,radio,input]){"
            "node.dispatchEvent(new Event('input',{bubbles:true}));"
            "node.dispatchEvent(new Event('change',{bubbles:true}));}"
            "button.click();"
            "return JSON.stringify({ok:true,range:input.value});"
            "})()",
            timeout=15,
        )
    )
    if not state.get("ok") or state.get("range") != range_text:
        raise WorkflowError(f"NDL download panel rejected the range: {state}")

    deadline = time.time() + timeout
    source: Optional[Path] = None
    while time.time() < deadline:
        candidates = []
        for path in downloads.glob("*.pdf"):
            stat = path.stat()
            old = before.get(path.resolve())
            if old is None or old != (stat.st_mtime_ns, stat.st_size):
                if stat.st_mtime >= clicked_at - 1:
                    candidates.append(path)
        recent_partials = [
            path
            for pattern in ("*.crdownload", "*.download")
            for path in downloads.glob(pattern)
            if path.stat().st_mtime >= clicked_at - 1
        ]
        if candidates and not recent_partials:
            source = max(candidates, key=lambda path: path.stat().st_mtime_ns)
            time.sleep(1)
            break
        time.sleep(1)
    if source is None:
        raise WorkflowError("The NDL panel did not create a new PDF in Downloads")
    shutil.copy2(source, destination)


def pending_rows(
    rows: List[Dict[str, Any]],
    pid: Optional[str],
    limit: Optional[int],
    all_rows: bool,
    include_review: bool,
) -> List[Dict[str, Any]]:
    allowed = {"pending", "error", "page_count_mismatch"}
    if include_review:
        allowed.add("needs_review")
    selected = [row for row in rows if row.get("status", "pending") in allowed]
    if pid:
        selected = [row for row in selected if str(row.get("pid")) == pid]
    elif selected and not all_rows:
        first_pid = str(selected[0].get("pid"))
        selected = [row for row in selected if str(row.get("pid")) == first_pid]
    return selected[:limit] if limit else selected


def invalid_name(output: Path, pages: int) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = output.with_name(f"{output.stem}.invalid-{stamp}-{pages}pages.pdf")
    sequence = 2
    while candidate.exists():
        candidate = output.with_name(
            f"{output.stem}.invalid-{stamp}-{pages}pages-{sequence}.pdf"
        )
        sequence += 1
    return candidate


def download(args: argparse.Namespace) -> int:
    plan = read_json(args.plan)
    rows = plan_rows(plan)
    selected = pending_rows(
        rows,
        args.pid,
        args.limit,
        args.all,
        args.include_review,
    )
    preview = [
        {
            "pid": row.get("pid"),
            "range": f"{row.get('start')}-{row.get('end')}",
            "filename": row.get("filename"),
            "status": row.get("status"),
        }
        for row in selected
    ]
    if not args.execute:
        print(json.dumps({"dry_run": True, "rows": preview}, ensure_ascii=False, indent=2))
        return 0

    require_macos()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    current_pid = None
    completed = 0
    for row in selected:
        pid = str(row.get("pid") or "")
        start = int(row.get("start"))
        end = int(row.get("end"))
        expected = int(row.get("expected_pages") or (end - start + 1))
        if not pid.isdigit() or start < 1 or end < start or end - start + 1 > 50:
            raise WorkflowError(f"Invalid plan row: {row!r}")
        output = safe_output_path(args.output_dir, str(row.get("filename") or ""))
        if output.exists():
            actual = pdf_page_count(output)
            if actual == expected:
                row["status"] = "downloaded"
                row.pop("error", None)
                write_json_atomic(args.plan, plan)
                completed += 1
                continue
            if not args.replace_invalid:
                raise WorkflowError(
                    f"{output.name} exists with {actual} pages; expected {expected}. "
                    "Use --replace-invalid to preserve it under an invalid-* name and retry."
                )
            os.replace(output, invalid_name(output, actual))

        if pid != current_pid:
            activate_pid(pid, open_missing=True)
            wait_for_pid(pid)
            current_pid = pid
        temporary = output.with_name(f".{output.stem}.{time.time_ns()}.part.pdf")
        try:
            mode = page_mode()
            print(
                json.dumps(
                    {
                        "pid": pid,
                        "range": f"{start}-{end}",
                        "filename": output.name,
                        "mode": mode,
                        "status": "requested",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if mode == "panel":
                panel_download(start, end, temporary, args.timeout)
            else:
                previous = current_pdf_link()
                open_print_modal()
                request_print_range(start, end)
                link = wait_for_generated_link(previous, args.timeout)
                fetch_generated_pdf(link, temporary)
            actual = pdf_page_count(temporary)
            if actual != expected:
                failed = invalid_name(output, actual)
                os.replace(temporary, failed)
                row["status"] = "page_count_mismatch"
                row["error"] = f"expected {expected} pages, got {actual}; saved as {failed.name}"
                write_json_atomic(args.plan, plan)
                raise WorkflowError(row["error"])
            os.replace(temporary, output)
            row["status"] = "downloaded"
            row["downloaded_at"] = datetime.now(timezone.utc).isoformat()
            row["actual_pages"] = actual
            row.pop("error", None)
            write_json_atomic(args.plan, plan)
            completed += 1
            print(
                json.dumps(
                    {"pid": pid, "filename": output.name, "pages": actual, "status": "downloaded"},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if args.delay:
                time.sleep(args.delay)
        except Exception as exc:
            if temporary.exists():
                temporary.unlink()
            if row.get("status") != "page_count_mismatch":
                row["status"] = "error"
                row["error"] = str(exc)
                write_json_atomic(args.plan, plan)
            if not args.continue_on_error:
                raise
            print(
                json.dumps(
                    {"pid": pid, "filename": output.name, "status": "error", "error": str(exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    print(json.dumps({"selected": len(selected), "completed": completed}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspector-js",
        help="Print browser JavaScript for non-macOS browser-control adapters",
    )
    inspect_parser.set_defaults(handler=lambda _args: print(INSPECTOR_JS) or 0)

    search_js_parser = subparsers.add_parser(
        "search-results-js",
        help="Print JavaScript that extracts PID candidates from an NDL search page",
    )
    search_js_parser.set_defaults(handler=lambda _args: print(SEARCH_RESULTS_JS) or 0)

    search_parser = subparsers.add_parser(
        "search-results",
        help="Save PID candidates from the active NDL search-results page",
    )
    search_parser.add_argument("--output", type=Path, default=Path("ndl-candidates.json"))
    search_parser.add_argument("--append", action="store_true")
    search_parser.set_defaults(handler=search_results)

    collect_parser = subparsers.add_parser(
        "collect",
        help="Inspect NDL PID tabs open in macOS Chrome",
    )
    collect_parser.add_argument("--output", type=Path, default=Path("ndl-inspection.json"))
    collect_parser.add_argument("--url-file", type=Path)
    collect_parser.add_argument("--pid", action="append")
    collect_parser.add_argument("--open-missing", action="store_true")
    collect_parser.set_defaults(handler=collect)

    download_parser = subparsers.add_parser(
        "download",
        help="Execute reviewed plan rows using the visible NDL page controls",
    )
    download_parser.add_argument("--plan", required=True, type=Path)
    download_parser.add_argument("--output-dir", required=True, type=Path)
    download_parser.add_argument("--pid")
    download_parser.add_argument("--limit", type=int)
    download_parser.add_argument("--all", action="store_true")
    download_parser.add_argument("--include-review", action="store_true")
    download_parser.add_argument("--replace-invalid", action="store_true")
    download_parser.add_argument("--continue-on-error", action="store_true")
    download_parser.add_argument("--delay", type=float, default=2.0)
    download_parser.add_argument("--timeout", type=int, default=300)
    download_parser.add_argument(
        "--execute",
        action="store_true",
        help="Required to perform downloads; otherwise print a dry run",
    )
    download_parser.set_defaults(handler=download)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
