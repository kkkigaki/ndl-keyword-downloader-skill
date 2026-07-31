# Data Schemas

## Contents

- Inspection input
- Plan output
- Override input
- Review rules

## Inspection input

`ndl_plan.py` accepts a JSON array. Each item uses this shape:

```json
{
  "pid": "10230307",
  "url": "https://dl.ndl.go.jp/pid/10230307",
  "title": "Example title",
  "author": "平田晋策 著",
  "publisher": "Example publisher",
  "publication_date": "1936",
  "doi": "10.11501/10230307",
  "access_scope": "送信サービスで閲覧可能",
  "total_frames": 140,
  "toc": [
    {
      "text": "Article title / 平田晋策",
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

- `schema_version`: currently `1`;
- `keywords`, `label`, and `rules`: reproducibility metadata;
- `rows`: ordered download operations;
- `skipped`: inspected items without a structured match or manually excluded.

Every row has a stable range, expected page count, safe basename, and status. Supported statuses are:

- `pending`: reviewed enough to download;
- `needs_review`: range cannot be established safely;
- `downloaded`: official PDF passed page-count verification;
- `error`: UI or transfer failed;
- `page_count_mismatch`: downloaded file did not match the planned range;
- `missing`, `unreadable`: audit findings.

Treat `start` and `end` as NDL frame numbers, inclusive. They are not printed page numbers.

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
        "label": "Article title / 平田晋策",
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
