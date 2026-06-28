#!/usr/bin/env python3
"""
Convert final_report.md → final_report_print.html → final_report.pdf

Steps:
  1. Parses the Markdown with mistune
  2. Embeds diagram PNGs as base64 data URIs so Chromium can find them
  3. Wraps in a clean print-ready HTML template
  4. Calls Chromium headless to produce the PDF

Usage:
  python3 md_to_pdf.py
"""
import base64, os, re, subprocess, sys

# ── paths ────────────────────────────────────────────────────────────────────
HERE    = os.path.dirname(os.path.abspath(__file__))
MD_IN   = os.path.join(HERE, "final_report.md")
HTML_OUT= os.path.join(HERE, "final_report_print.html")
PDF_OUT = os.path.join(HERE, "final_report.pdf")

# ── markdown → html ──────────────────────────────────────────────────────────
import mistune
md_text = open(MD_IN).read()
body_html = mistune.html(md_text)

# ── inline images as base64 ──────────────────────────────────────────────────
def embed_image(m):
    src = m.group(1)
    abs_src = os.path.join(HERE, src)
    if os.path.exists(abs_src):
        ext  = abs_src.rsplit(".", 1)[-1].lower()
        mime = {"png": "image/png", "jpg": "image/jpeg", "svg": "image/svg+xml"}.get(ext, "image/png")
        data = base64.b64encode(open(abs_src, "rb").read()).decode()
        return f'src="data:{mime};base64,{data}"'
    return m.group(0)

body_html = re.sub(r'src="([^"]+)"', embed_image, body_html)

# ── html template ─────────────────────────────────────────────────────────────
CSS = """
  @import url('https://fonts.googleapis.com/css2?family=Merriweather:ital,wght@0,400;0,700;1,400&family=JetBrains+Mono:wght@400;600&display=swap');

  *, *::before, *::after { box-sizing: border-box; }
  html { font-size: 11pt; }
  body {
    font-family: 'Merriweather', Georgia, 'Times New Roman', serif;
    color: #1a1a1a;
    background: #fff;
    line-height: 1.75;
    max-width: 820px;
    margin: 0 auto;
    padding: 32px 40px 60px;
  }

  /* ── headings ── */
  h1 { font-size: 2.2em; line-height: 1.2; margin: 0 0 6px; color: #111; border-bottom: 3px solid #c62828; padding-bottom: 10px; }
  h2 { font-size: 1.55em; margin: 48px 0 12px; color: #111; border-bottom: 1px solid #e0e0e0; padding-bottom: 6px; page-break-after: avoid; break-after: avoid; }
  h3 { font-size: 1.15em; margin: 28px 0 8px; color: #333; page-break-after: avoid; break-after: avoid; }
  h1 + p, h2 + p, h3 + p { margin-top: 0; }

  /* ── subtitle block (bold + italic lines after h1) ── */
  h1 + p strong { font-size: 1em; }

  /* ── paragraphs & lists ── */
  p  { margin: 0 0 14px; }
  ul, ol { margin: 0 0 14px; padding-left: 26px; }
  li { margin-bottom: 6px; }
  li > ul, li > ol { margin-top: 4px; margin-bottom: 0; }

  /* ── horizontal rule ── */
  hr { border: none; border-top: 1px solid #ddd; margin: 36px 0; }

  /* ── blockquotes (key learnings) ── */
  blockquote {
    margin: 16px 0;
    padding: 12px 18px;
    border-left: 4px solid #c62828;
    background: #fff8f7;
    color: #333;
    page-break-inside: avoid; break-inside: avoid;
  }
  blockquote p { margin: 0; }

  /* ── inline code ── */
  code {
    font-family: 'JetBrains Mono', 'Fira Code', Menlo, monospace;
    font-size: 0.82em;
    background: #f3f3f3;
    border: 1px solid #e0e0e0;
    border-radius: 3px;
    padding: 1px 5px;
    color: #c0392b;
  }
  pre > code { display: block; padding: 12px 16px; white-space: pre-wrap; color: #1a1a1a; }

  /* ── tables ── */
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9em;
    margin: 16px 0 24px;
    page-break-inside: avoid; break-inside: avoid;
  }
  th {
    background: #c62828;
    color: #fff;
    text-align: left;
    padding: 9px 14px;
    font-weight: 700;
    font-size: 0.85em;
    letter-spacing: .03em;
  }
  td {
    padding: 8px 14px;
    border-bottom: 1px solid #e8e8e8;
    vertical-align: top;
  }
  tr:nth-child(even) td { background: #fafafa; }
  tr:last-child td { border-bottom: none; }

  /* ── images ── */
  img {
    max-width: 100%;
    height: auto;
    display: block;
    margin: 16px auto 6px;
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    page-break-inside: avoid; break-inside: avoid;
  }
  img + em { display: block; text-align: center; font-size: 0.82em; color: #777; margin-bottom: 20px; }

  /* ── links ── */
  a { color: #c62828; text-decoration: none; }
  a:hover { text-decoration: underline; }

  /* ── page breaks ── */
  h2 { page-break-before: always; break-before: always; }
  h2:first-of-type { page-break-before: auto; break-before: auto; }

  /* ── print ── */
  @media print {
    *, *::before, *::after { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }
    body { padding: 0; max-width: 100%; }
    a { color: #c62828 !important; }
  }
"""

HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Multi-Agent Knowledge Base System — Final Capstone Report</title>
<style>{CSS}</style>
</head>
<body>
{body_html}
</body>
</html>
"""

open(HTML_OUT, "w").write(HTML)
print(f"HTML written → {HTML_OUT}")

# ── chromium pdf export ───────────────────────────────────────────────────────
CHROMIUM = next(
    (p for p in [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/opt/homebrew/bin/chromium",
    ] if os.path.exists(p)),
    None
)
if not CHROMIUM:
    print("Chromium not found — open final_report_print.html in Chrome and print to PDF manually.")
    sys.exit(0)

import tempfile
tmpdir = tempfile.mkdtemp()

cmd = [
    CHROMIUM,
    "--headless=new",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--no-first-run",
    f"--user-data-dir={tmpdir}",
    f"--print-to-pdf={PDF_OUT}",
    "--no-pdf-header-footer",
    "--run-all-compositor-stages-before-draw",
    "--virtual-time-budget=8000",
    f"file://{HTML_OUT}",
]
print(f"Exporting PDF → {PDF_OUT}")
result = subprocess.run(cmd, capture_output=True, text=True)
if result.returncode == 0:
    print(f"Done → {PDF_OUT}")
else:
    print("Chromium error (run from your own terminal):")
    print(result.stderr[-800:] if result.stderr else "(no stderr)")
    print(f"\nFallback: open {HTML_OUT} in Chrome and use ⌘P → Save as PDF")
