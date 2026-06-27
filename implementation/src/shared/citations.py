"""Citation post-processing for background-mode RAG answers.

The auto-RAG generator emits prose with inline ``[S1]`` / ``[S2]`` markers
plus (despite the system prompt) a trailing plain-text ``SOURCES:`` block.
That format is fine for the chatbot path — the orchestrator strips the
markers in ``_polish_chat_answer`` so the chat UI never sees them — but
when the same answer is dropped into a Markdown documentation page during
enrichment, the markers and the text-only footer render as inert text
instead of clickable references.

:func:`link_citations` rewrites the answer for the doc-page consumer:

  * any LLM-emitted ``SOURCES:`` / ``Sources:`` footer is stripped;
  * each ``[Sn]`` marker (and grouped ``[Sn, Sm]`` markers) is rewritten
    as ``[Sn](relative/path/to/source.md)`` so the citation is clickable
    inline.

Query-mode answers MUST NOT call this — the chat polish pass already
removes the markers and would just see the new ``](path)`` fragments.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any


_LLM_SOURCES_FOOTER_RX = re.compile(
    # Match a trailing block that starts with "SOURCES:" / "Sources:" /
    # "SOURCE:" optionally preceded by whitespace, then everything to the
    # end of the answer. The leading space tolerance matches the format
    # the local LLM tends to emit (``\n SOURCES:\n[S1]...``).
    r"\n\s*\bSOURCES?\b\s*:?[ \t]*\n[\s\S]*\Z",
    re.IGNORECASE,
)

_INLINE_MARKER_RX = re.compile(
    # ``[S1]`` / ``[S 1]`` / ``[S1, S2]`` / ``[S1, 2]``. The negative
    # lookahead on ``(`` prevents us from re-linking a marker that was
    # already converted to ``[S1](path)`` on a prior call.
    r"\[\s*S\s*(\d+(?:\s*[,;]\s*S?\s*\d+)*)\s*\](?!\()"
)


def _normalise_page_dir(page_uri: str) -> str:
    """Return the directory of ``page_uri`` relative to the docs root.

    Page URIs can arrive in two shapes — specialist-relative
    (``sd/database/foo.md``) or repo-rooted (``documentation/sd/...``).
    Both collapse to the same domain-rooted directory after stripping
    the optional ``documentation/`` prefix.
    """
    p = (page_uri or "").lstrip("/")
    if p.startswith("documentation/"):
        p = p[len("documentation/"):]
    parent = posixpath.dirname(p)
    return parent or "."


def _source_full_uri(source: dict[str, Any]) -> str:
    """Normalise a RAG source entry to its domain-rooted URI.

    The auto-RAG response carries ``domain`` ('bp' / 'sd') and
    ``source_uri`` (which may or may not already include the domain
    prefix depending on which specialist indexed the chunk). This
    function returns the joined form so :func:`posixpath.relpath` has
    something stable to work with.
    """
    uri = (source.get("source_uri") or "").lstrip("/")
    domain = (source.get("domain") or "").strip().lower()
    if not uri:
        return ""
    if uri.startswith(("bp/", "sd/")) or uri.startswith("source-code://"):
        return uri
    if domain in {"bp", "sd"}:
        return f"{domain}/{uri}"
    return uri


def link_citations(
    answer: str,
    *,
    sources: list[dict[str, Any]],
    page_uri: str,
) -> str:
    """Rewrite a background-mode RAG answer with clickable citations.

    Behaviour is conservative — when there are no sources, or no
    inline markers, we return the input largely unchanged (still
    stripping any LLM-emitted plain-text SOURCES footer so we don't
    leave a half-formatted block behind).
    """
    if not answer:
        return answer

    # 1) Strip any LLM-emitted plain-text "SOURCES:" footer first; we
    #    rebuild our own formatted version below.
    cleaned = _LLM_SOURCES_FOOTER_RX.sub("", answer).rstrip()

    if not sources:
        # No sources to link against — drop any orphan ``[Sn]`` markers
        # so the page doesn't render dead references.
        cleaned = _INLINE_MARKER_RX.sub("", cleaned)
        return re.sub(r"[ \t]{2,}", " ", cleaned).rstrip()

    page_dir = _normalise_page_dir(page_uri)
    # links[i] = (full_uri, rel_link) for [S(i+1)]. ``rel_link`` is "" when
    # the source can't be linked (e.g. ``source-code://`` synthetic).
    links: list[tuple[str, str]] = []
    for src in sources:
        full = _source_full_uri(src)
        if not full or full.startswith("source-code://"):
            links.append((full, ""))
            continue
        try:
            rel = posixpath.relpath(full, page_dir)
        except ValueError:
            rel = full
        links.append((full, rel))

    # 2) Replace inline ``[Sn]`` / ``[Sn, Sm]`` markers with Markdown
    #    links pointing at the relative path.
    def _replace(match: re.Match[str]) -> str:
        nums = [int(n) for n in re.findall(r"\d+", match.group(1))]
        parts: list[str] = []
        for n in nums:
            if 1 <= n <= len(links):
                full, rel = links[n - 1]
                if rel:
                    parts.append(f"[S{n}]({rel})")
                elif full:
                    # Source exists but isn't linkable (synthetic URI) —
                    # keep the marker as plain text so the footer can
                    # still describe it.
                    parts.append(f"[S{n}]")
                # else: marker points at a source we don't have — drop it.
            else:
                parts.append(f"[S{n}]")
        return ", ".join(parts) if parts else ""

    rewritten = _INLINE_MARKER_RX.sub(_replace, cleaned)
    return re.sub(r"[ \t]{2,}", " ", rewritten).rstrip()


# ---------------------------------------------------------------------------
# Cross-domain reference extraction (feeds the §9.7 dashboard rollup)
# ---------------------------------------------------------------------------

# Markdown link in inline form: ``[label](target)``. We deliberately
# accept any ``target`` shape and filter on prefix below.
_MD_LINK_RX = re.compile(r"\[[^\]]+\]\(([^)\s#]+)(?:\s+\"[^\"]*\")?\)")


def extract_cross_domain_refs(
    content: str,
    *,
    my_domain: str,
    page_uri: str,
) -> list[str]:
    """Return cross-domain page URIs referenced from a Markdown page.

    The dashboard's ``cross_reference_health`` rollup reads SD's
    ``referenced_products`` and BP's ``referenced_services`` doc-index
    columns. New pages set those at compose time, but the
    enrich-existing path historically just carried forward whatever was
    there before — so any cross-domain link the LLM (or an SME) drops
    in afterwards never makes it onto the dashboard.

    This helper rebuilds the list from the page itself: it scans every
    Markdown link, normalises it back to a domain-rooted URI relative
    to ``page_uri``'s directory, and returns the deduped set whose
    domain prefix is *not* ``my_domain``. The BP-side caller passes
    ``my_domain="bp"`` and gets SD URIs back; the SD-side caller does
    the inverse.
    """
    if not content or my_domain not in {"bp", "sd"}:
        return []
    other = "sd" if my_domain == "bp" else "bp"
    page_dir = _normalise_page_dir(page_uri)
    refs: set[str] = set()
    for match in _MD_LINK_RX.finditer(content):
        target = match.group(1).strip()
        if not target or target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        # Resolve the relative link against the page's directory and
        # strip any ``documentation/`` prefix the author hard-coded.
        joined = posixpath.normpath(posixpath.join(page_dir, target))
        if joined.startswith("documentation/"):
            joined = joined[len("documentation/"):]
        if joined.startswith(f"{other}/"):
            refs.add(joined)
    return sorted(refs)
