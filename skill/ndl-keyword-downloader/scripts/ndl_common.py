#!/usr/bin/env python3
"""Shared helpers for the NDL keyword download workflow."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List


class WorkflowError(RuntimeError):
    """Raised when an input or artifact violates a workflow invariant."""


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkflowError(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"Invalid JSON in {path}: {exc}") from exc


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def plan_rows(plan: Any) -> List[Dict[str, Any]]:
    if isinstance(plan, list):
        return plan
    if isinstance(plan, dict) and isinstance(plan.get("rows"), list):
        return plan["rows"]
    raise WorkflowError("Plan must be a JSON object with a rows array")


def safe_output_path(output_dir: Path, filename: str) -> Path:
    if not filename or Path(filename).name != filename:
        raise WorkflowError(f"Unsafe output filename: {filename!r}")
    candidate = (output_dir / filename).resolve()
    root = output_dir.resolve()
    if candidate.parent != root:
        raise WorkflowError(f"Output escapes the destination directory: {filename!r}")
    return candidate


def pdf_page_count(path: Path) -> int:
    """Return PDF page count using the first available reliable reader."""
    errors = []
    try:
        from pypdf import PdfReader  # type: ignore

        return len(PdfReader(str(path)).pages)
    except Exception as exc:
        errors.append(f"pypdf: {exc}")

    try:
        from PyPDF2 import PdfReader  # type: ignore

        return len(PdfReader(str(path)).pages)
    except Exception as exc:
        errors.append(f"PyPDF2: {exc}")

    try:
        import fitz  # type: ignore

        with fitz.open(path) as document:
            return document.page_count
    except Exception as exc:
        errors.append(f"PyMuPDF: {exc}")

    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo:
        result = subprocess.run(
            [pdfinfo, str(path)],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("Pages:"):
                    return int(line.split(":", 1)[1].strip())
        errors.append(f"pdfinfo: {result.stderr.strip() or 'no Pages field'}")

    raise WorkflowError(
        "Could not count PDF pages. Install pypdf (`python3 -m pip install pypdf`) "
        f"or Poppler pdfinfo. Attempts: {'; '.join(errors)}"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
