"""Assemble the Vercel deployment in dist/vercel/ and print the deploy command.

Only what the hosted viewer needs is copied: the cartographer package, a
compacted snapshot of the results store, the analyst report sidecar, the
entry point and config from deploy/vercel/. Keys (env/), tests, papers and
scratch stores never enter the bundle.

    python3 scripts/build_vercel.py [results/campaign.sqlite]
    cd dist/vercel && vercel deploy --prod
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dist" / "vercel"


def main() -> None:
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "results" / "campaign.sqlite"
    # Keep the CLI's project link (.vercel/) so a rebuild deploys to the same
    # project instead of creating a new one.
    link = OUT / ".vercel"
    saved = None
    if link.exists():
        saved = ROOT / "dist" / ".vercel-link"
        shutil.rmtree(saved, ignore_errors=True)
        shutil.move(str(link), saved)
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    if saved is not None:
        shutil.move(str(saved), link)

    shutil.copytree(ROOT / "cartographer", OUT / "cartographer",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "deploy" / "vercel", OUT, dirs_exist_ok=True)
    (OUT / "docs").mkdir()
    shutil.copy2(ROOT / "docs" / "capability-report.json", OUT / "docs")
    for name in ("LICENSE", "README.md"):
        shutil.copy2(ROOT / name, OUT / name)
    (OUT / "requirements.txt").write_text("# stdlib only\n")
    (OUT / ".python-version").write_text("3.12\n")

    # A consistent, compacted snapshot: VACUUM INTO reads through sqlite, so a
    # campaign appending to the live store cannot tear the copy.
    (OUT / "results").mkdir()
    snap = OUT / "results" / "campaign.sqlite"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    src.execute("VACUUM INTO ?", (str(snap),))
    src.close()
    con = sqlite3.connect(snap)
    con.execute("PRAGMA journal_mode=DELETE")
    n = con.execute("SELECT count(*) FROM experiments").fetchone()[0]
    con.close()

    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file()) / 1e6
    print(f"dist/vercel ready: {n:,} records, {size:.1f} MB")
    print("deploy: cd dist/vercel && vercel deploy --prod")


if __name__ == "__main__":
    main()
