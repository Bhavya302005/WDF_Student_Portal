from __future__ import annotations

import argparse
import json
from pathlib import Path

from .factory import local_stack


def process_file(path: Path) -> dict[str, int]:
    repository, processor, _ = local_stack()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("input must be a JSON array of jobs")
    changed = reused = failed = 0
    for raw in payload:
        try:
            _, artifacts = processor.process(raw)
            if artifacts: changed += 1
            else: reused += 1
        except (TypeError, ValueError):
            failed += 1
    return {"processed": changed, "reused": reused, "failed": failed, "ready": len(repository.ready_jobs())}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build global job intelligence from a JSON job export")
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    print(json.dumps(process_file(args.input), sort_keys=True))


if __name__ == "__main__":
    main()
