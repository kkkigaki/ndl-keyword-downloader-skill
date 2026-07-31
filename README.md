# NDL Keyword Downloader Skill

A Codex Skill for building a reproducible corpus from Japan's National Diet Library Digital Collections (`dl.ndl.go.jp`).

It inspects open NDL item pages, matches a person or topic against structured titles, authors, and tables of contents, creates a reviewable download plan, drives NDL's visible print-generated PDF workflow, splits requests at 50 frames, includes an optional safety frame after articles, resumes interrupted batches, and verifies every PDF.

This project does **not** bypass access controls and does not read browser cookies, saved passwords, or profile databases. Users remain responsible for NDL terms, access eligibility, copyright, and permitted reuse.

## Features

- Whole-item downloads for configured title or author matches
- Search-result candidate capture with query-URL provenance
- TOC-based article extraction with one extra frame by default
- Ordered 50-frame segmentation for long books
- False-positive resistance: raw page text and `関連の資料` are not matching sources
- JSON plan, TSV checklist, manual overrides, resumable status
- Temporary-file validation before official filenames
- Page-count, SHA-256, duplicate-name, missing-file, and extra-file audits
- macOS Chrome adapter plus a browser-neutral workflow

## Install

Clone the repository and copy or symlink the skill folder into your Codex skills directory:

```bash
git clone https://github.com/kkkigaki/ndl-keyword-downloader-skill.git
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
ln -s "$PWD/ndl-keyword-downloader-skill/skill/ndl-keyword-downloader" \
  "${CODEX_HOME:-$HOME/.codex}/skills/ndl-keyword-downloader"
python3 -m pip install pypdf
```

Restart Codex after installation. Then invoke:

```text
Use $ndl-keyword-downloader to inspect my open NDL tabs and download sources related to 平田晋策.
```

## Quick start

Run commands from `skill/ndl-keyword-downloader`.

Collect open macOS Chrome tabs:

```bash
python3 scripts/ndl_macos_chrome.py search-results \
  --output /absolute/work/ndl-candidates.json

python3 scripts/ndl_macos_chrome.py collect \
  --url-file /absolute/work/ndl-candidates.json \
  --open-missing \
  --output /absolute/work/ndl-inspection.json
```

Chrome must have **View > Developer > Allow JavaScript from Apple Events** enabled.

Build the plan:

```bash
python3 scripts/ndl_plan.py \
  --inspection /absolute/work/ndl-inspection.json \
  --keyword 平田晋策 \
  --label 平田晋策 \
  --author-match person \
  --plan /absolute/work/ndl-download-plan.json \
  --checklist /absolute/work/ndl-download-checklist.tsv \
  --review /absolute/work/ndl-needs-review.json
```

Review the plan and unresolved items. The first download command is a dry run:

```bash
python3 scripts/ndl_macos_chrome.py download \
  --plan /absolute/work/ndl-download-plan.json \
  --output-dir /absolute/work/pdfs
```

Pass historical variants separately, for example `--keyword 松岡洋右 --keyword 松岡外務大臣`. The planner does not silently expand names because titles and offices can be ambiguous.

Execute:

```bash
python3 scripts/ndl_macos_chrome.py download \
  --plan /absolute/work/ndl-download-plan.json \
  --output-dir /absolute/work/pdfs \
  --all --execute
```

Audit:

```bash
python3 scripts/ndl_audit.py \
  --plan /absolute/work/ndl-download-plan.json \
  --output-dir /absolute/work/pdfs \
  --report /absolute/work/ndl-audit.json \
  --hash --find-extras
```

## Platform support

Planning and auditing are cross-platform Python 3.9+ commands. The bundled live-browser adapter supports macOS Google Chrome because it reuses the user's existing authorized session through Apple Events. On other platforms, use a browser-control connector and the browser-neutral sequence documented inside the Skill.

The NDL interface is not a stable automation API. Selectors can change, and a human must review ambiguous matches and access warnings.

## 中文说明

这个 Skill 把“按关键词检查国会图书馆页面、判断整本或单篇、每次最多下载 50 面、文章多带一面、检查重复与漏页”的流程打包成了可复用工具。它只使用用户有权访问的可见网页流程，不读取账号密码或浏览器 Cookie。自动规划后仍应人工检查 `needs-review` 文件，尤其是目录最后一篇、同名人物和“相关资料”造成的语义歧义。

## Development

```bash
python3 -m unittest discover -s tests -v
python3 /path/to/skill-creator/scripts/quick_validate.py \
  skill/ndl-keyword-downloader
```

## License

MIT. This license covers the code in this repository, not materials downloaded from NDL.
