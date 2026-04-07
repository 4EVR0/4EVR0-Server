from app.domain.user import UserProfile
from app.services.taxonomy_normalization_service import (
    normalize_skin_types,
    normalize_concerns,
    normalize_constraints,
    infer_effects,
)


def extract_profile(message: str) -> UserProfile:
    skin_types = normalize_skin_types(message)
    concerns = normalize_concerns(message)
    effects = infer_effects(concerns)
    constraints = normalize_constraints(message)

    return UserProfile(
        skin_types=skin_types,
        concerns=concerns,
        effects=effects,
        constraints=constraints,
    )
