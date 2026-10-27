"""Run explicitly selected real-model cases and retain first-pass/final evidence.

Example: python scripts/evaluate_semantic_extraction.py --case mixed_frontmatter
No production books or learning records are modified. Repeat without --new to resume.
"""

import argparse
import json
from pathlib import Path

from teaching_agent.application import Application
from teaching_agent.backends import BackendRegistry
from teaching_agent.extraction_models import PIPELINE
from teaching_agent.models import Book, Chunk, Section
from teaching_agent.storage import Store, dumps, writer_lock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, help="fixture case id, or all")
    parser.add_argument("--data-dir", type=Path, default=Path("tmp/semantic-evaluation"))
    parser.add_argument("--config", type=Path, default=Path("teach.toml"))
    parser.add_argument("--new", action="store_true")
    args = parser.parse_args()
    fixtures = Path(__file__).resolve().parents[1] / "tests/fixtures/semantic_cases.json"
    cases = json.loads(fixtures.read_text(encoding="utf-8"))
    cases = [c for c in cases if args.case == "all" or c["id"] == args.case]
    if not cases:
        parser.error("unknown case")
    with writer_lock(args.data_dir):
        store = Store(args.data_dir)
        store.initialize()
        app = Application(store, backends=BackendRegistry(args.config))
        for case in cases:
            book = Book(
                id="eval_" + case["id"],
                sha256="eval_" + case["id"],
                filename=case["id"] + ".txt",
                page_count=1,
                sections=[
                    Section(id="s1", title="实验样本", start_page=1, end_page=1, chunk_ids=["c1"])
                ],
                chunks=[Chunk(id="c1", page=1, text=case["text"])],
            )
            with store.connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO books VALUES(?,?,?)",
                    (book.id, book.sha256, book.model_dump_json()),
                )
            index = app.library.index(book.id, lambda msg: print(msg, flush=True), new=args.new)
            steps = store.rows("SELECT step,data FROM book_steps WHERE owner_id=?", (index["id"],))
            events = store.rows(
                "SELECT data FROM events WHERE owner_id=? AND kind='book_operation_completed'",
                (index["id"],),
            )
            report = {
                "case": case,
                "index_id": index["id"],
                "pipeline": PIPELINE,
                "first_pass": [
                    json.loads(s["data"])
                    for s in steps
                    if f":{PIPELINE}:extract:" in s["step"] and not s["step"].endswith(":rejected")
                ],
                "final": json.loads(index["data"]),
                "calls": [json.loads(e["data"]) for e in events],
                "manual_review_required": True,
            }
            output = args.data_dir / (case["id"] + "-report.json")
            output.write_text(dumps(report), encoding="utf-8")
            print(output.resolve(), flush=True)


if __name__ == "__main__":
    main()
