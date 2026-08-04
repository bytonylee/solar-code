"""Internal documentation."""

from dataclasses import dataclass

from . import model


@dataclass(frozen=True)
class CacheProfile:
    """Internal documentation."""

    model: str
    chunk_tokens: int
    min_prefix_tokens: int
    promotion: str
    measured_at: str
    note: str = ""

    @property
    def measured(self) -> bool:
        """Internal documentation."""
        return bool(self.measured_at)



PROFILES: dict[str, CacheProfile] = {
    "solar-open2": CacheProfile(
        model="solar-open2",
        chunk_tokens=1_088,
        min_prefix_tokens=1_088,
        promotion="eager",
        measured_at="2026-08-03",
        note="19라운드 성장 프로브 84.3%. 경계에서 lag=-1 선행 승격이 잦다."),
    "solar-pro4": CacheProfile(
        model="solar-pro4",
        chunk_tokens=1_088,
        min_prefix_tokens=1_088,
        promotion="laggy",
        measured_at="2026-08-03",
        note="19라운드 성장 프로브 80.1%. 승격이 한 턴 늦는 경우가 산발적."),
}


def default_profile(name: str) -> CacheProfile:
    """Internal documentation."""
    return CacheProfile(
        model=name,
        chunk_tokens=model.CACHE_CHUNK_TOKENS,
        min_prefix_tokens=model.CACHE_MIN_PREFIX_TOKENS,
        promotion="steady",
        measured_at="",
        note="미측정 모델. experiments/cache_growth_probe.py로 갱신할 것.")


def for_model(name: str | None = None) -> CacheProfile:
    """Internal documentation."""
    name = name or model.MODEL
    found = PROFILES.get(name)
    return found if found is not None else default_profile(name)


def observed(profile: CacheProfile, ledger) -> CacheProfile:
    """Internal documentation."""
    seen = getattr(ledger, "lags", None)
    if not seen:
        return profile
    promotion = ledger.promotion
    if promotion == profile.promotion:
        return profile
    return CacheProfile(
        model=profile.model,
        chunk_tokens=profile.chunk_tokens,
        min_prefix_tokens=profile.min_prefix_tokens,
        promotion=promotion,
        measured_at=profile.measured_at,
        note=f"런타임 관측으로 {profile.promotion} -> {promotion}")
