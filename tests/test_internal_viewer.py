"""개발자용 내부 조회 API: 꺼짐 기본값, Tailscale 대역만 허용, 응답 방식 필터."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api import internal
from app.core.config import settings
from app.main import app

ROWS = [{"turn_id": "t1", "created_at": datetime(2026, 11, 8, 13, 0, tzinfo=timezone.utc), "response_mode": "no_products",
         "transport": "stream", "release_id": "r1", "question": "폼클렌징 추천해줘", "answer": "아직 없어요",
         "products": '[{"product_name": "A"}, {"brand": "x"}]'}]


def client_from(host):
    return TestClient(app, client=(host, 50000))


def test_disabled_by_default_is_404():
    with patch.object(settings, "internal_viewer_enabled", False):
        assert client_from("100.101.1.2").get("/internal/turn-records").status_code == 404


def test_only_tailnet_clients_allowed():
    listing = AsyncMock(return_value=ROWS)
    with patch.object(settings, "internal_viewer_enabled", True), \
            patch.object(internal.repo, "list_turn_records", listing):
        assert client_from("203.0.113.5").get("/internal/turn-records").status_code == 404   # 공인 IP(Caddy 경유 포함)
        assert client_from("172.30.90.2").get("/internal/turn-records").status_code == 404   # 프록시 내부 IP
        r = client_from("100.101.1.2").get("/internal/turn-records?mode=no_products&mode=generated,quality_fallback&days=3&limit=5")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert listing.await_args.kwargs == {"modes": ["no_products", "generated", "quality_fallback"], "days": 3, "limit": 5}
    body = r.json()
    assert body["records"][0]["products"] == ["A"] and "subject_id" not in body["records"][0]


def test_limits_validated_and_modes_summary():
    counts = AsyncMock(return_value={"generated": 10})
    with patch.object(settings, "internal_viewer_enabled", True), \
            patch.object(internal.repo, "count_turn_records_by_mode", counts):
        c = client_from("100.101.1.2")
        assert c.get("/internal/turn-records?limit=1000").status_code == 422
        assert c.get("/internal/turn-records/modes?days=7").json() == {"days": 7, "modes": {"generated": 10}}
