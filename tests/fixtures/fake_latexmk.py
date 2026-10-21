"""A stand-in for `latexmk`: writes a PDF with as many blank pages as the
source asks for (a `% FAKE_PAGES=N` line, default 1). `% FAKE_TEX_FAIL`
makes it fail like a LaTeX error. Appends its argv to `$FAKE_LATEX_LOG`."""
import json
import os
import re
import sys
from pathlib import Path

from pypdf import PdfWriter

args = sys.argv[1:]
if os.environ.get("FAKE_LATEX_LOG"):
    with open(os.environ["FAKE_LATEX_LOG"], "a") as f:
        f.write(json.dumps({"argv": args, "cwd": os.getcwd(), "env_openin": os.environ.get("openin_any")}) + "\n")
outdir = next(a.split("=", 1)[1] for a in args if a.startswith(("-outdir=", "-output-directory=")))
source = Path(next(a for a in args if a.endswith(".tex")))
text = source.read_text()
print(f"Latexmk: fake run on {source}")
if "FAKE_TEX_FAIL" in text:
    print(f"./{source.name}:3: Undefined control sequence.")
    sys.exit(12)
m = re.search(r"FAKE_PAGES=(\d+)", text)
w = PdfWriter()
for _ in range(int(m.group(1)) if m else 1):
    w.add_blank_page(width=612, height=792)
with open(Path(outdir) / f"{source.stem}.pdf", "wb") as f:
    w.write(f)
