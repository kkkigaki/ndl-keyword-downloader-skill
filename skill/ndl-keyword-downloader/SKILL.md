---
name: ndl-keyword-downloader
description: Plan, download, archive, and verify keyword-relevant historical sources from the National Diet Library Digital Collections (国立国会図書館デジタルコレクション). Use when a user asks to inspect many open NDL PID pages, find a person or topic in titles/authors/tables of contents, download whole matching items or relevant chapters through the site's print-generated PDF workflow, split ranges at the NDL 50-frame limit, include a safety frame after an article, preserve bibliographic metadata and 転載時の表記例, create an Excel reference archive, avoid duplicate downloads, or audit the resulting PDFs.
---

# NDL Keyword Downloader

Turn open NDL item pages or PID URLs into a reviewable download plan, execute that plan through the visible NDL controls, archive completed sources in Excel, and prove that every resulting PDF has the expected frame count.

## Boundaries

- Access only items the user may lawfully view. Do not bypass NDL access controls, CAPTCHA, geographic restrictions, or download limits.
- Use the user's already-open browser session only when the task authorizes browser control. Never read cookies, passwords, browser storage, or credential files.
- Operate the visible `印刷` or download-panel workflow. A generated `PDFファイルを開く` URL may be fetched after the page creates it; do not reverse-engineer private endpoints.
- Keep each requested range at 50 frames or fewer. Pace repeated requests and stop if NDL displays a warning or changes its terms.
- Treat copyright and permitted reuse separately from technical access. Preserve DOI, access scope, and `転載時の表記例` text when available.

## Workflow

### 1. Define the matching policy

Record:

- one or more keyword variants;
- the output directory and filename label;
- whether title and author matches mean whole-item download;
- the number of safety frames after a TOC article, default `1`;
- the maximum range size, default and hard maximum `50`.

Use `--author-match person` when a person name must match a bibliographic author after removing role suffixes such as `著` or `編`. Use `contains` when organizational or compound author fields should also count.

### 2. Discover and collect structured item data

If the user has not already opened candidate item tabs, search through the visible NDL interface with each exact keyword and historical variant. Preserve the query URL and active filters. On macOS, save PID candidates from the active result page:

```bash
python3 scripts/ndl_macos_chrome.py search-results \
  --output /absolute/work/ndl-candidates.json
```

Use `--append` after moving to another result page or variant query. Review the candidate list before opening a large number of items.

On macOS with Chrome, run from this skill directory:

```bash
python3 scripts/ndl_macos_chrome.py collect \
  --url-file /absolute/work/ndl-candidates.json \
  --open-missing \
  --output /absolute/work/ndl-inspection.json
```

This inspects currently open `https://dl.ndl.go.jp/pid/...` tabs and does not read browser secrets. Chrome must allow JavaScript from Apple Events.

For another browser controller or operating system, read [browser-workflow.md](references/browser-workflow.md). Generate the page inspector with:

```bash
python3 scripts/ndl_macos_chrome.py inspector-js
```

Save all results as one JSON array conforming to [schema.md](references/schema.md).

### 3. Build and review the plan

```bash
python3 scripts/ndl_plan.py \
  --inspection /absolute/work/ndl-inspection.json \
  --keyword XXXX \
  --label XXXX \
  --author-match person \
  --plan /absolute/work/ndl-download-plan.json \
  --checklist /absolute/work/ndl-download-checklist.tsv \
  --review /absolute/work/ndl-needs-review.json
```

The planner applies these rules:

- Download the whole item when the configured title or author field matches.
- Otherwise use only matching TOC entries. Ignore raw body-text hits, which often come from `関連の資料`.
- End an article immediately before the next distinct TOC frame, then add the configured safety frame. With the default, the download includes the next TOC frame.
- Split every range into ordered chunks of at most 50 frames.
- Mark a final TOC hit `needs_review` when no following entry establishes its end.
- Mark suspicious one-frame TOC gaps and adjacent keyword-bearing TOC entries for review; they often represent nested captions or serialized subentries rather than article boundaries.
- Disambiguate duplicate filenames with the PID.

Inspect the checklist and `needs-review` file before downloading. Resolve false positives or uncertain ranges with an override file described in [schema.md](references/schema.md), then rebuild the plan. Do not use `--include-review` merely to suppress unresolved review work.

Supply historical variants as separate `--keyword` arguments, for example `--keyword XXXX --keyword XXXX旧字表記`. The planner does not infer equivalent names, titles, or offices without evidence because that would create false positives.

### 4. Dry-run and download

First show the exact pending operations:

```bash
python3 scripts/ndl_macos_chrome.py download \
  --plan /absolute/work/ndl-download-plan.json \
  --output-dir /absolute/work/pdfs
```

After review, execute one PID or the full batch:

```bash
python3 scripts/ndl_macos_chrome.py download \
  --plan /absolute/work/ndl-download-plan.json \
  --output-dir /absolute/work/pdfs \
  --archive /absolute/work/ndl-reference-archive.xlsx \
  --all --execute
```

The adapter reuses valid existing PDFs, updates the plan after every successful file, validates a temporary PDF before making it official, and refreshes the Excel archive after every verified PDF. It refuses stale generated links and preserves page-count mismatches under an `invalid-*` filename.

If macOS Chrome control is unavailable, follow the manual/browser-controller sequence in [browser-workflow.md](references/browser-workflow.md), keeping filenames and statuses synchronized with the plan.

### 5. Verify the Excel reference archive

The automatic workbook groups a whole item or article into one row even when its PDFs were split into 50-frame chunks. Confirm that each row includes:

- title or periodical title, article title when applicable, author, publisher, publication date, and volume/issue;
- target frames without the safety frame and downloaded frames with it;
- every downloaded filename, verified page total, PID URL, DOI, call number, bibliographic ID, and access scope;
- the verbatim `転載時の表記例` and an automatically formatted reference draft.

The reference draft is not authoritative. Reconcile it with the original `転載時の表記例` and the target journal's style. Never relabel NDL frame numbers as printed page numbers.

For downloads completed by another browser controller, build or refresh the workbook explicitly:

```bash
python3 scripts/ndl_archive.py \
  --plan /absolute/work/ndl-download-plan.json \
  --output-dir /absolute/work/pdfs \
  --archive /absolute/work/ndl-reference-archive.xlsx
```

### 6. Audit completion

Install `pypdf` or Poppler `pdfinfo`, then run:

```bash
python3 scripts/ndl_audit.py \
  --plan /absolute/work/ndl-download-plan.json \
  --output-dir /absolute/work/pdfs \
  --report /absolute/work/ndl-audit.json \
  --hash --find-extras
```

Completion requires all plan rows to be verified, no duplicate planned filenames, matching expected/actual total page counts, and an Excel archive row for every completed source or article. Explain all `needs_review`, missing, mismatched, unreadable, extra, or unarchived files rather than declaring success from file count alone.

## Recovery

- If Chrome switches windows, reactivate the intended PID and re-inspect the URL before clicking.
- If a generated link does not change, retry generation; never reuse the old link for a new range.
- If the page selectors drift, inspect the visible controls and read [browser-workflow.md](references/browser-workflow.md). Update the adapter only after confirming the new UI behavior.
- If interrupted, rerun the same plan. Valid PDFs are skipped by page-count verification.
