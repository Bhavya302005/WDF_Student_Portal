"""Merge parallel artifacts into the latest branch lists by canonical board ID."""
from dataclasses import replace
from pathlib import Path
import sys

from discover_commoncrawl import HERE, PLATFORMS, _known, _save_companies


def merge(folder: Path) -> None:
    for platform in PLATFORMS.values():
        artifact = folder / platform.csv_path.relative_to(HERE)
        if not artifact.exists():
            continue
        incoming, _ = _known(replace(platform, csv_path=artifact))
        current, _ = _known(platform)
        _save_companies(platform, current + incoming, [])
        print(f'{platform.key}: merged {len(incoming)} artifact rows')


if __name__ == '__main__':
    merge(Path(sys.argv[1]))
