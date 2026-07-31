# Data Schemas

## Contents

- Inspection input
- Plan output
- Excel archive output
- Override input
- Review rules

## Inspection input

`ndl_plan.py` accepts a JSON array. Each item uses this shape:

```json
{
  "pid": "10230307",
  "url": "https://dl.ndl.go.jp/pid/10230307",
  "title": "Example title",
  "author": "XXXX 著",
  "publisher": "Example publisher",
  "publication_date": "1936",
  "volume_issue": "第1巻第2号",
  "call_number": "Example call number",
  "bibliographic_id": "Example bibliographic ID",
  "doi": "10.11501/10230307",
  "access_scope": "送信サービスで閲覧可能",
  "total_frames": 140,
  "toc": [
    {
      "text": "Article title / XXXX",
      "frame": 49,
      "href": "https://dl.ndl.go.jp/pid/10230307/1/49"
    },
    {
      "text": "Following article",
      "frame": 57,
      "href": "https://dl.ndl.go.jp/pid/10230307/1/57"
    }
  ],
  "reproduction_note": "Visible text near 転載時の表記例"
}
```

Required fields are `pid`, `title`, `author`, `total_frames`, and `toc`. Empty title or author values are allowed. `total` and `pubdate` are accepted as legacy aliases for `total_frames` and `publication_date`.

Do not include cookies, account identifiers, passwords, local-storage values, full page source, or unrelated body text.

## Plan output

The planner writes an object with:

- `schema_version`: currently `2`;
- `keywords`, `label`, and `rules`: reproducibility metadata;
- `rows`: ordered download operations;
- `skipped`: inspected items without a structured match or manually excluded.

Every row has a stable range, expected page count, safe basename, archive key, bibliographic metadata, and status. Whole-item chunks share one `archive_key`. Article chunks also share one `archive_key` and preserve `article_start` plus `target_end`, allowing the Excel archive to distinguish the article's target range from the extra downloaded safety frame.

- `pending`: reviewed enough to download;
- `needs_review`: range cannot be established safely;
- `downloaded`: official PDF passed page-count verification;
- `error`: UI or transfer failed;
- `page_count_mismatch`: downloaded file did not match the planned range;
- `missing`, `unreadable`: audit findings.

Treat `start` and `end` as NDL frame numbers, inclusive. They are not printed page numbers.

The planner carries these inspection fields into every applicable row: `title`, `article_title`, `author`, `publisher`, `publication_date`, `volume_issue`, `call_number`, `bibliographic_id`, `doi`, `access_scope`, `reproduction_note`, and `captured_at`.

## Excel archive output

`ndl_macos_chrome.py download --execute` automatically writes
`OUTPUT_DIR/ndl-reference-archive.xlsx` unless `--archive` selects another path. The
standalone `ndl_archive.py` command rebuilds the same workbook after manual or
browser-neutral downloads.

The workbook contains one row per whole item or article, not one row per 50-frame PDF
chunk. Its principal columns are:

- book or periodical title, article title, author, publisher, publication date, and volume/issue;
- target frame range, downloaded frame range including the safety frame, filenames, and verified pages;
- PID, NDL URL, DOI, call number, bibliographic ID, and access scope;
- verbatim `転載時の表記例`;
- an automatically formatted reference draft and download time.

Treat the formatted reference as a draft. Preserve the original reproduction text, verify
the target journal's style, and never represent NDL frame numbers as printed page numbers.

## Override input

Overrides are a JSON object keyed by PID. Pass the path with `--overrides`.

Exclude a false positive:

```json
{
  "1193192": {
    "action": "exclude"
  }
}
```

Force whole-item download:

```json
{
  "10230307": {
    "action": "full"
  }
}
```

Supply verified article boundaries:

```json
{
  "10230307": {
    "action": "ranges",
    "ranges": [
      {
        "start": 49,
        "end": 56,
        "label": "Article title / XXXX",
        "extra_after": 1
      }
    ]
  }
}
```

For a manual range, `end` is the article's last target frame before the safety-frame addition. Set `extra_after` to `0` only when the supplied end already includes the safety frame.

## Review rules

Review is mandatory when:

- a whole-item match has no reliable total-frame value;
- a matching TOC entry has no following entry and `--trust-last-toc` was not explicitly chosen;
- the next TOC frame is only one frame later, suggesting a nested caption or subentry;
- another keyword-bearing TOC entry is within two frames, suggesting a multipart or nested article;
- a PID is duplicated in the inspection input;
- an override range is malformed;
- a keyword appears only in unstructured page text or `関連の資料`;
- a title, author, or TOC match is semantically ambiguous.
