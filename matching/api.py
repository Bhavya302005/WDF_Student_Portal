from __future__ import annotations

from .resume import MAX_RESUME_BYTES, CandidateIntakeService, resume_text_from_bytes
from .intake import IntakeRecommendationService
from .schemas import (CandidateIntakeResponse, IntakeAnswersRequest,
                      CandidateProfile, CandidateProfileDraft, RecommendationActionRequest,
                      RecommendationHistoryResponse, RecommendationSeenRequest,
                      RecommendationRequest, RecommendationResponse,
                      RecommendationSummary, UserRecommendation,
                      ResumeRecommendationRequest, ResumeRecommendationResponse,
                      ResumeTextRequest)
from .service import RecommendationService
from .auth import AuthConfigurationError, InvalidAuthToken, authenticated_user_id
from .user_state import MemoryUserStateStore, UserStateStore


def create_app(service: RecommendationService, intake: CandidateIntakeService | None = None,
               allowed_origins: list[str] | None = None,
               user_state: UserStateStore | None = None):
    """Create the API without forcing scraper-only installations to import FastAPI."""
    try:
        from fastapi import FastAPI, File, HTTPException, Request, UploadFile
        from fastapi.middleware.cors import CORSMiddleware
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("Install the 'api' dependencies to serve recommendations") from exc

    # ``from __future__ import annotations`` resolves endpoint annotations from
    # module globals; publish this optional dependency only after the guarded import.
    globals()["UploadFile"] = UploadFile
    globals()["Request"] = Request

    app = FastAPI(title="JobRadar Matching API", version="1.0")
    state = user_state or MemoryUserStateStore()
    app.state.user_state = state
    origins = allowed_origins or []
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "OPTIONS"], allow_headers=["Content-Type", "Authorization", "X-User-ID"])

    def verify_user(http_request: Request, supplied_user_id: str) -> str:
        try:
            authenticated = authenticated_user_id(http_request.headers.get("Authorization"))
        except InvalidAuthToken as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except AuthConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if authenticated is not None and authenticated != supplied_user_id:
            raise HTTPException(status_code=403, detail="The requested user does not match the signed-in account")
        return authenticated or supplied_user_id

    @app.post("/api/v1/recommendations/jobs", response_model=RecommendationResponse)
    def recommend(payload: RecommendationRequest, request: Request) -> RecommendationResponse:
        verify_user(request, payload.user_id)
        response = service.recommend(payload)
        response.recommendations = state.record_recommendations(
            payload.user_id, response.profile_hash, response.algorithm_version,
            response.recommendations,
        )
        return response

    @app.get("/api/v1/recommendations/jobs/history", response_model=RecommendationHistoryResponse)
    def recommendation_history(request: Request, user_id: str, include_dismissed: bool = False,
                               limit: int = 200) -> RecommendationHistoryResponse:
        verify_user(request, user_id)
        values = state.recommendations(user_id, include_dismissed=include_dismissed,
                                       limit=max(1, min(limit, 1000)))
        return RecommendationHistoryResponse(
            recommendations=values,
            unread_count=sum(item.is_new for item in values),
        )

    @app.post("/api/v1/recommendations/jobs/seen", response_model=RecommendationSummary)
    def recommendations_seen(payload: RecommendationSeenRequest, request: Request) -> RecommendationSummary:
        verify_user(request, payload.user_id)
        state.mark_seen(payload.user_id, payload.job_ids)
        return state.summary(payload.user_id)

    @app.patch("/api/v1/recommendations/jobs/{job_id}/status", response_model=UserRecommendation)
    def recommendation_status(job_id: str, payload: RecommendationActionRequest,
                              request: Request) -> UserRecommendation:
        verify_user(request, payload.user_id)
        result = state.set_status(payload.user_id, job_id, payload.status)
        if result is None:
            raise HTTPException(status_code=404, detail="Recommendation not found for this account")
        return result

    @app.get("/api/v1/recommendations/summary", response_model=RecommendationSummary)
    def recommendation_summary(request: Request, user_id: str) -> RecommendationSummary:
        verify_user(request, user_id)
        return state.summary(user_id)

    @app.get("/api/v1/candidate-profile/latest", response_model=CandidateProfile | None)
    def latest_candidate_profile(request: Request, user_id: str) -> CandidateProfile | None:
        verify_user(request, user_id)
        return state.latest_profile(user_id)

    @app.put("/api/v1/candidate-profile", response_model=CandidateProfile)
    def save_candidate_profile(payload: CandidateProfile, request: Request) -> CandidateProfile:
        """Reprocess and persist edits to an existing candidate profile."""
        user_id = verify_user(request, payload.user_id)
        processed = service.profiles.process(user_id, payload)
        state.save_profile(processed)
        return processed

    @app.get("/api/v1/candidate-profile/draft", response_model=CandidateProfileDraft | None)
    def latest_candidate_profile_draft(request: Request, user_id: str) -> CandidateProfileDraft | None:
        verify_user(request, user_id)
        return state.latest_profile_draft(user_id)

    @app.put("/api/v1/candidate-profile/draft", response_model=CandidateProfileDraft)
    def save_candidate_profile_draft(payload: CandidateProfileDraft,
                                     request: Request) -> CandidateProfileDraft:
        verify_user(request, payload.user_id)
        return state.save_profile_draft(payload)

    if intake is not None:
        integrated = IntakeRecommendationService(intake, service)

        @app.post("/api/v1/candidate-profile/parse", response_model=CandidateIntakeResponse)
        def parse_resume_text(payload: ResumeTextRequest, request: Request) -> CandidateIntakeResponse:
            user_id = verify_user(request, payload.user_id)
            return intake.parse_text(user_id, payload.resume_text)

        @app.post("/api/v1/candidate-profile/parse-file", response_model=CandidateIntakeResponse)
        async def parse_resume_file(request: Request, user_id: str, file: UploadFile = File(...)) -> CandidateIntakeResponse:
            try:
                user_id = verify_user(request, user_id)
                content = await file.read(MAX_RESUME_BYTES + 1)
                text = resume_text_from_bytes(file.filename or "resume.txt", content)
                return intake.parse_text(user_id, text)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        @app.post("/api/v1/candidate-profile/apply-answers", response_model=CandidateIntakeResponse)
        def apply_answers(payload: IntakeAnswersRequest, request: Request) -> CandidateIntakeResponse:
            try:
                verify_user(request, payload.intake.user_id)
                confirmed = intake.apply_answers(payload.intake, payload.answers)
                state.save_profile(confirmed.profile)
                return confirmed
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        @app.post("/api/v1/recommendations/jobs/from-resume", response_model=ResumeRecommendationResponse)
        def recommend_from_resume(payload: ResumeRecommendationRequest, request: Request) -> ResumeRecommendationResponse:
            try:
                verify_user(request, payload.user_id)
                result = integrated.recommend(payload)
                state.save_profile(result.intake.profile)
                result.recommendations.recommendations = state.record_recommendations(
                    payload.user_id,
                    result.recommendations.profile_hash,
                    result.recommendations.algorithm_version,
                    result.recommendations.recommendations,
                )
                return result
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/health/matching")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
