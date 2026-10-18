from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Iterable

from matching.hashing import normalize_text
from matching.hashing import stable_hash


class Taxonomy:
    def __init__(self, path: str | Path | None = None) -> None:
        source = Path(path) if path else Path(__file__).with_name("data.json")
        data = json.loads(source.read_text(encoding="utf-8"))
        self.version = stable_hash(data)
        self.aliases = {normalize_text(alias): canonical for canonical, aliases in data["skill_aliases"].items() for alias in aliases}
        self.related = {normalize_text(k): {normalize_text(v) for v in values} for k, values in data["related_skills"].items()}
        self.roles = data["roles"]
        terms = sorted(self.aliases, key=len, reverse=True)
        self.skill_pattern = re.compile(r"(?<![\w+#])(?:" + "|".join(re.escape(x) for x in terms) + r")(?![\w+#])", re.I)
        # Compiled once: classify_role runs for every candidate job and profile role.
        self._role_patterns = [
            (len(token), re.compile(r"(?<!\w)" + re.escape(token) + r"(?!\w)"), role["family"], role["subfamily"])
            for role in self.roles for token in (normalize_text(alias) for alias in role["aliases"]) if token
        ]
        self._classify = lru_cache(maxsize=65_536)(self._classify_uncached)

    def normalize_skill(self, skill: str) -> str:
        cleaned = normalize_text(skill)
        return self.aliases.get(cleaned, cleaned)

    def normalize_skills(self, values: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(self.normalize_skill(v) for v in values if normalize_text(v)))

    def extract_skills(self, text: str) -> list[str]:
        return self.normalize_skills(match.group() for match in self.skill_pattern.finditer(normalize_text(text)))

    def classify_role(self, title: str) -> tuple[str | None, str | None]:
        return self._classify(normalize_text(title))

    def _classify_uncached(self, cleaned: str) -> tuple[str | None, str | None]:
        # Longest matching alias wins; on ties the earliest-declared role wins.
        best: tuple[int, str, str] | None = None
        for length, pattern, family, subfamily in self._role_patterns:
            if (best is None or length > best[0]) and pattern.search(cleaned):
                best = (length, family, subfamily)
        return (best[1], best[2]) if best else (None, None)

    def relation(self, candidate_skill: str, job_skill: str) -> str:
        left, right = self.normalize_skill(candidate_skill), self.normalize_skill(job_skill)
        if left == right:
            return "EXACT"
        if right in self.related.get(left, set()) or left in self.related.get(right, set()):
            return "RELATED"
        return "MISSING"


@lru_cache(maxsize=1)
def default_taxonomy() -> Taxonomy:
    return Taxonomy()
