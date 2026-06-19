"""Input-doc normalisation + content hashing (§9.3.3 background mode).

Tiny but useful surface: B&P normalises every input doc the same way
before handing it to ``RAG_MCP.index`` so the content hash is stable
across cosmetic changes (whitespace, trailing newlines, BOM markers,
collapsed empty lines). The same hash backs the
:class:`BPSourcesInventory` "is this unchanged?" check, so deterministic
normalisation matters.

Production deployments will replace ``read_input`` with a richer
``GitHubPageStore.read_input`` that may also resolve embedded references
or strip HTML; the contract is identical so the rest of the pipeline
doesn't care.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


_BOM = "﻿"
_TRAIL_WS = re.compile(r"[ \t]+\n")
_BLANK_RUN = re.compile(r"\n{3,}")
_HEADING_RX = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class NormalizedDoc:
    source_uri: str
    text: str
    content_hash: str
    title: str | None


def normalize(text: str) -> str:
    """Collapse cosmetic noise so the same logical doc hashes the same.

    Steps:
      1. Strip a BOM if present.
      2. Normalise line endings to ``\\n``.
      3. Strip trailing whitespace on each line.
      4. Collapse 3+ consecutive blank lines into 2.
      5. Strip leading/trailing whitespace on the whole doc.
    """
    if text.startswith(_BOM):
        text = text[len(_BOM):]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAIL_WS.sub("\n", text)
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()


def content_hash(text: str) -> str:
    """SHA-1 over the *normalised* text. Short hex prefix is enough to
    distinguish revisions in the audit trail."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def first_h1(text: str) -> str | None:
    """Return the first markdown ``# Heading`` in the doc, or ``None``."""
    m = _HEADING_RX.search(text)
    return m.group(1).strip() if m else None


def normalize_input(*, source_uri: str, raw_text: str) -> NormalizedDoc:
    """One-shot: normalise + hash + best-effort title extraction."""
    text = normalize(raw_text)
    return NormalizedDoc(
        source_uri=source_uri,
        text=text,
        content_hash=content_hash(text),
        title=first_h1(text),
    )
