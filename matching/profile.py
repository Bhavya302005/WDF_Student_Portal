from __future__ import annotations

from typing import Any

from .config import MatchingSettings
from .embeddings import EmbeddingProvider
from .hashing import canonical_employment_type, canonical_work_mode, normalize_text, stable_hash
from .repository import MatchingRepository
from .schemas import CandidateProfile
from .taxonomy.normalizer import Taxonomy


class CandidateProcessor:
    def __init__(self, repository: MatchingRepository, embeddings: EmbeddingProvider,
                 taxonomy: Taxonomy, settings: MatchingSettings) -> None:
        self.repository, self.embeddings, self.taxonomy, self.settings = repository, embeddings, taxonomy, settings

    def process(self, user_id: str, raw: CandidateProfile | dict[str, Any]) -> CandidateProfile:
        profile_input = raw if isinstance(raw, CandidateProfile) else CandidateProfile.model_validate({**raw, "user_id": user_id})
        data = profile_input.model_dump(exclude={"embedding", "profile_hash", "embedding_model", "embedding_version"})
        data["user_id"] = user_id
        for key in ("skills", "professional_skills", "project_skills"):
            data[key] = self.taxonomy.normalize_skills(data.get(key, []))
        # Backward-compatible structured profiles historically used `roles` for
        # employment history. Resume intake supplies professional_roles explicitly.
        if not data.get("professional_roles"):
            data["professional_roles"] = list(data.get("roles", []))
        data["work_mode_preferences"] = [canonical_work_mode(value) for value in data.get("work_mode_preferences", [])]
        data["employment_preferences"] = [canonical_employment_type(value) for value in data.get("employment_preferences", [])]
        inferred_families, inferred_roles = [], []
        for role in data.get("roles", []):
            family, subfamily = self.taxonomy.classify_role(str(role))
            if family: inferred_families.append(family)
            if subfamily: inferred_roles.append(subfamily)
        professional_roles = list(data.get("professional_roles", []))
        for role in professional_roles.copy():
            family, subfamily = self.taxonomy.classify_role(str(role))
            if family: inferred_families.append(family)
            if subfamily and subfamily not in professional_roles: professional_roles.append(subfamily)
        data["role_families"] = list(dict.fromkeys(data.get("role_families", []) + inferred_families))
        data["roles"] = list(dict.fromkeys(data.get("roles", []) + inferred_roles))
        data["professional_roles"] = professional_roles
        identity = {k: v for k, v in data.items() if k not in {"user_id", "processing_version"}}
        profile_hash = stable_hash([identity, self.settings.processing_version, self.settings.embedding_version])
        with self.repository.job_lock("profile:" + profile_hash):
            cached = self.repository.get_profile(profile_hash)
            if cached:
                return cached.model_copy(update={"user_id": user_id})
            profile = CandidateProfile.model_validate(data)
            text = " | ".join(filter(None, ["skills " + " ".join(profile.skills), "roles " + " ".join(profile.roles),
                "professional roles " + " ".join(profile.professional_roles),
                "families " + " ".join(profile.role_families), "domains " + " ".join(profile.domains),
                "education " + " ".join(profile.education), "locations " + " ".join(profile.locations),
                f"professional experience {profile.professional_years} years" if profile.professional_years is not None else ""]))
            profile.profile_hash = profile_hash
            profile.embedding = self.embeddings.embed_query([normalize_text(text)])[0]
            profile.embedding_model = self.embeddings.model_name
            profile.embedding_version = self.settings.embedding_version
            self.repository.save_profile(profile)
            return profile
