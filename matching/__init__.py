"""Global, reusable job intelligence and candidate-specific recommendation."""

from .config import MatchingSettings
from .service import RecommendationService

__all__ = ["MatchingSettings", "RecommendationService"]
