"""Provider registry gating + mock provider determinism (§6.1, D4)."""
import asyncio

import pytest

from app.context import load_defaults
from app.errors import ProviderConfigError
from app.providers import SCHEMA_VECTOR_DIM, build_embedding_provider, build_vision_provider
from app.providers.base import RerankCandidate
from app.providers.mock import MOCK_EMBEDDING_MODEL, MOCK_VISION_MODEL, MockEmbeddingProvider, MockVisionProvider
from app.settings import Settings

from conftest import make_jpeg


def test_mock_embedding_deterministic_unit_norm():
    provider = MockEmbeddingProvider()
    data = make_jpeg(seed=3)
    v1 = asyncio.run(provider.embed_image(data))
    v2 = asyncio.run(provider.embed_image(data))
    v3 = asyncio.run(provider.embed_image(make_jpeg(seed=4)))
    assert v1 == v2, "identical bytes must embed identically"
    assert v1 != v3, "different images must not collide"
    assert len(v1) == SCHEMA_VECTOR_DIM == provider.dim
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_mock_mode_registry_picks_mock_providers(settings):
    cfg = load_defaults()
    embed = build_embedding_provider(settings, cfg)
    assert embed.model == MOCK_EMBEDDING_MODEL
    assert embed.dim == SCHEMA_VECTOR_DIM
    vision = build_vision_provider(settings, cfg)
    assert vision.name == "mock"
    assert vision.model == MOCK_VISION_MODEL


def test_vertex_without_creds_fails_loudly():
    s = Settings(app_mode="prod", embedding_provider="vertex", gcp_project_id="", gcp_sa_json="")
    with pytest.raises(ProviderConfigError):
        build_embedding_provider(s, {})


def test_titan_refused_until_reembed_migration():
    s = Settings(app_mode="prod", embedding_provider="titan")
    with pytest.raises(ProviderConfigError, match="re-embed|retype|migration"):
        build_embedding_provider(s, {})


def test_prod_mode_without_vision_keys_fails_loudly():
    s = Settings(app_mode="prod", vision_provider="auto", gemini_api_key="", openai_api_key="")
    with pytest.raises(ProviderConfigError):
        build_vision_provider(s, {})


def test_text_only_mode_disables_llm():
    s = Settings(app_mode="mock", rerank_mode="text_only")
    assert build_vision_provider(s, {}) is None


def test_mock_vision_rerank_hooks():
    vision = MockVisionProvider(force_category="not_a_lamp")
    candidates = [RerankCandidate(sku="LMP-1", title="Dome pendant", thumbs=None,
                                  attributes={"type": "pendant", "finish": "chrome", "color": "white"})]
    result = asyncio.run(vision.rerank(make_jpeg(), candidates))
    assert result.query_attributes["category"] == "not_a_lamp"
    assert result.query_attributes["confidence"] == 0.2  # below the E6 gate
    match = result.matches["LMP-1"]
    assert 0.0 <= match["attribute_match"] <= 1.0
    assert len(match["reason"]) <= 120


def test_mock_vision_default_confidence_clears_gate():
    vision = MockVisionProvider()
    candidates = [RerankCandidate(sku="LMP-1", title="t", thumbs=None, attributes={"type": "table"})]
    result = asyncio.run(vision.rerank(make_jpeg(), candidates))
    assert result.query_attributes["confidence"] >= 0.5


def test_mock_vision_enrich_deterministic():
    vision = MockVisionProvider()
    product = {"sku": "LMP-1001", "title": "x", "category": "pendant", "attributes": {}}
    out1 = asyncio.run(vision.enrich(product, None))
    out2 = asyncio.run(vision.enrich(product, None))
    assert out1 == out2
    assert set(out1) <= {"type", "style", "finish", "color", "size_class"}


def test_registry_settings_reads_env_at_construction(monkeypatch):
    # RERANK_MODE is env-driven; explicit kwargs must win for tests
    s = Settings(app_mode="mock", rerank_mode="vision")
    vision = build_vision_provider(s, load_defaults())
    assert vision is not None
