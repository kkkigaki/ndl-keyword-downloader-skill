# Browser Workflow and UI Adapters

## Contents

- Adapter selection
- Search and candidate discovery
- Browser-neutral collection
- Browser-neutral download sequence
- Excel reference archive
- Current macOS Chrome selectors
- Failure handling

## Adapter selection

Prefer, in order:

1. a browser connector that can use the user's existing NDL tabs and login state;
2. `scripts/ndl_macos_chrome.py` on macOS Chrome;
3. visible manual interaction while maintaining the generated plan.

Do not launch a separate unauthenticated browser when the requested items depend on the user's authorized NDL session. Do not obtain the session by reading browser profile files.

## Search and candidate discovery

1. Use NDL's visible search form with the exact keyword.
2. Repeat with user-approved historical orthographies, abbreviations, and titles. Do not invent identity equivalences.
3. Preserve the result-page URL, search filters, result count, and capture date.
4. On each result page, run the JavaScript printed by:

   ```bash
   python3 scripts/ndl_macos_chrome.py search-results-js
   ```

5. Deduplicate candidates by PID and review the list before opening item pages.

On macOS Chrome, `search-results --output ...` captures the active page and `--append` merges later pages. Search results identify candidates only; title/author/TOC inspection determines the download plan.

## Browser-neutral collection

1. List open tabs and retain only URLs matching `https://dl.ndl.go.jp/pid/<digits>`.
2. Deduplicate by PID.
3. Activate each item page and wait for the PID URL and document readiness.
4. Run the JavaScript printed by:

   ```bash
   python3 scripts/ndl_macos_chrome.py inspector-js
   ```

5. Parse each returned JSON string and append the object to one inspection array.
6. Save progress after every PID so an interruption does not discard earlier inspection work.

The inspector intentionally extracts structured metadata and same-PID TOC links. It does not use raw body keyword matches because the page's `関連の資料` section creates false positives.

## Browser-neutral download sequence

For each reviewed plan row:

1. Activate `https://dl.ndl.go.jp/pid/<pid>` and verify the active URL.
2. Confirm the output does not already exist with the expected page count.
3. Open `印刷`, or use the visible PDF download panel when that is the interface NDL presents.
4. Choose the specific-frame option and enter the inclusive `start-end` range.
5. Confirm the range is no more than 50 frames.
6. Start file generation.
7. Wait for `印刷用ファイルを作成しました。右のリンクからPDFファイルを表示できます。`
8. Open `PDFファイルを開く`; do not reuse a link left from a previous range.
9. Download to a temporary filename.
10. Count PDF pages and require `end - start + 1`.
11. Rename to the planned filename only after validation.
12. Mark that row `downloaded` and persist the plan.

When operating manually, use the checklist as the transaction log. Never infer success solely from Chrome's download indicator.

## Excel reference archive

After the selected download rows are complete, generate the same archive used by the
macOS adapter:

```bash
python3 scripts/ndl_archive.py \
  --plan /absolute/work/ndl-download-plan.json \
  --output-dir /absolute/work/pdfs \
  --archive /absolute/work/ndl-reference-archive.xlsx
```

Confirm that one workbook row represents each whole item or article, including all PDF
chunks. Preserve the visible `転載時の表記例`; for an article, also preserve its title,
target NDL frame range, and downloaded range including the safety frame.

## Current macOS Chrome selectors

The adapter currently recognizes:

- print openers: `#open-printing-modal`, `#open-internet-printing-modal`, or a visible `印刷` button;
- specific range radios: `#printing-modal-range-specific` or `#range-specific`;
- range input: `#range-specific-input`;
- create button: `#printing-modal-printing-button`, `#internet-printing-modal-printing-button`, or a button mentioning print-file creation;
- generated link: anchor text containing `PDFファイルを開く`;
- panel controls: `#file-type-pdf`, `#range-specific`, `#panel-download-download-button`.

Selectors are implementation details, not a stable NDL API. Reconfirm visible labels and behavior after a site update.

## Failure handling

- **Wrong window or tab:** reactivate by PID and verify `location.href` before continuing.
- **No new generated link:** retry the same range. Never accept the prior link as evidence.
- **Page-count mismatch:** preserve the failed PDF under `invalid-*`; leave the row unresolved.
- **Existing invalid official file:** stop by default. Use `--replace-invalid` only after reviewing what will be renamed.
- **CAPTCHA or access warning:** stop and return control to the user.
- **Panel download ambiguity:** accept only a new or modified PDF created after the click, not the newest historical file in `Downloads`.
- **Interrupted batch:** rerun the unchanged plan; the adapter verifies and skips complete files.
