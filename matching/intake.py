from __future__ import annotations

from .resume import CandidateIntakeService
from .schemas import (RecommendationRequest, ResumeRecommendationRequest,
                      ResumeRecommendationResponse)
from .service import RecommendationService


class IntakeRecommendationService:
    def __init__(self, intake: CandidateIntakeService, recommendations: RecommendationService) -> None:
        self.intake, self.recommendations = intake, recommendations

    def recommend(self, request: ResumeRecommendationRequest) -> ResumeRecommendationResponse:
        parsed = self.intake.parse_text(request.user_id, request.resume_text)
        confirmed = self.intake.apply_answers(parsed, request.answers)
        profile = confirmed.profile
        roles = list(profile.roles) if profile.strict_role_constraint else []
        for role in roles.copy():
            _, canonical = self.intake.parser.taxonomy.classify_role(role)
            if canonical and canonical not in roles:
                roles.append(canonical)
        locations = profile.locations if profile.hard_location_constraint else []
        response = self.recommendations.recommend(RecommendationRequest(
            user_id=request.user_id, profile=profile, limit=request.limit,
            roles=roles, locations=locations,
            work_modes=(profile.work_mode_preferences if profile.strict_work_mode_constraint else []),
            employment_types=(profile.employment_preferences if profile.strict_employment_constraint else []),
        ))
        return ResumeRecommendationResponse(intake=confirmed, recommendations=response)
