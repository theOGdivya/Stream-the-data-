"""Build the clickable preview: the real dashboard pages + recorded demo answers.

    python tools/preview/build.py <recording-dir> <out-dir>

<recording-dir> comes from tools/preview/record.js (index.json + data/). The output is a
static folder: index.html (landing page), the dashboard pages (Live as live.html), and
preview/ with mock.js and the recorded answers (JSON inlined into one index file).
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "server" / "aqserver" / "static"
HERE = Path(__file__).resolve().parent
VIEWPORT = '<meta name="viewport" content="width=device-width, initial-scale=1">'


def main(rec_dir: Path, out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(STATIC, out)
    (out / "index.html").rename(out / "live.html")
    for f in list(out.rglob("*.html")) + list((out / "js").glob("*.js")):
        text = f.read_text(encoding="utf-8")
        text = text.replace("'index.html'", "'live.html'").replace('"index.html"', '"live.html"')
        if f.suffix == ".html":
            assert VIEWPORT in text, f"no viewport meta in {f.name}"
            text = text.replace(VIEWPORT, VIEWPORT + '\n  <script src="preview/mock.js"></script>', 1)
        if f.name == "results.html":  # the recorded run is short: open on 5-minute averages
            text = text.replace('<option value="3600" selected>', '<option value="3600">').replace(
                '<option value="300">', '<option value="300" selected>')
        f.write_text(text, encoding="utf-8")

    data_out = out / "preview" / "data"
    data_out.mkdir(parents=True)
    shutil.copy(HERE / "mock.js", out / "preview" / "mock.js")
    raw = json.loads((rec_dir / "index.json").read_text(encoding="utf-8"))
    used = set()
    for e in raw["entries"]:
        src = rec_dir / "data" / e["f"]
        e.pop("body", None)
        if "json" in e["h"].get("content-type", ""):
            e["j"] = json.loads(src.read_text(encoding="utf-8"))
            del e["f"]
        else:
            used.add(e["f"])
    for name in used:
        shutil.copy(rec_dir / "data" / name, data_out / name)
    (data_out / "index.json").write_text(json.dumps(raw, separators=(",", ":")), encoding="utf-8")
    (out / "index.html").write_text((HERE / "landing.html").read_text(encoding="utf-8"), encoding="utf-8")
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    files = sum(1 for p in out.rglob("*") if p.is_file())
    print(f"preview built in {out}: {files} files, {size / 1e6:.1f} MB")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
