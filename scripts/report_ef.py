"""Evidence Fidelity Campaign matrix from the store (read-only, no LLM).

    python3 scripts/report_ef.py --run belief=camp-ef-belief-gemma4qat-001 --run tickets=camp-ef-tickets-gemma4qat-001 \
        [--db results/campaign.sqlite] [--out docs/reports/evidence-fidelity-<label>-<date>.md]
"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from cartographer.report.evidence_matrix import matrix_from_store  # noqa: E402
from cartographer.store.store import ResultsStore  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--run", action="append", required=True, help="family=run_id (repeatable)")
ap.add_argument("--db", default="results/campaign.sqlite")
ap.add_argument("--out")
a = ap.parse_args()
runs = dict(x.split("=", 1) for x in a.run)
md = matrix_from_store(ResultsStore.open_read_only(a.db), runs)
if a.out:
    pathlib.Path(a.out).write_text(md)
    print(f"wrote {a.out}")
else:
    print(md)
