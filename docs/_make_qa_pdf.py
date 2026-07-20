#!/usr/bin/env python3
"""Render candidate_qa_bank_ad_event.md -> styled A4 PDF (weasyprint)."""
from pathlib import Path
import markdown
from weasyprint import HTML

SRC = Path("/root/projects/hr-radar/docs/candidate_qa_bank_ad_event.md")
OUT = Path("/root/projects/hr-radar/docs/candidate_qa_bank_ad_event.pdf")

md_text = SRC.read_text(encoding="utf-8")
body = markdown.markdown(md_text, extensions=["extra", "sane_lists"])

CSS = """
@page {
  size: A4;
  margin: 1.8cm 1.9cm 2.0cm 1.9cm;
  @bottom-center {
    content: "Банк вопросов · Аккаунт-директор Event · HR Radar";
    font-size: 8pt; color: #9ca3af;
  }
  @bottom-right { content: counter(page) " / " counter(pages); font-size: 8pt; color: #9ca3af; }
}
* { box-sizing: border-box; }
body {
  font-family: "DejaVu Sans", "Noto Color Emoji", sans-serif;
  font-size: 10.5pt; line-height: 1.5; color: #1f2937;
}
h1 {
  font-size: 19pt; color: #7c2d12; margin: 0 0 4px 0; line-height: 1.25;
  border-bottom: 3px solid #ea580c; padding-bottom: 8px;
}
h2 {
  font-size: 13.5pt; color: #c2410c; margin: 22px 0 8px 0;
  padding: 6px 10px; background: #fff7ed; border-left: 4px solid #ea580c;
  border-radius: 3px; page-break-after: avoid;
}
h3 {
  font-size: 11.5pt; color: #1e3a8a; margin: 14px 0 4px 0; page-break-after: avoid;
}
p { margin: 5px 0; }
ul { margin: 5px 0 8px 0; padding-left: 20px; }
li { margin: 3px 0; }
strong { color: #111827; }
em { color: #6b7280; }
hr { border: none; border-top: 1px solid #e5e7eb; margin: 16px 0; }
h3 { page-break-inside: avoid; }
"""

html = f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{body}</body></html>"
HTML(string=html).write_pdf(str(OUT))
print(f"OK -> {OUT} ({OUT.stat().st_size} bytes)")
