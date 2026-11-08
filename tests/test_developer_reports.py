"""사이드바 '개발자에게 의견 보내기' API."""
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.services import consent_policy, data_collection as collection

SUBJECT = "b" * 32


def client():
    c = TestClient(app)
    c.cookies.set(collection.SUBJECT_COOKIE, SUBJECT)
    c.cookies.set("4evr0_session", "sess-1")
    return c


def _post(body, *, owns=True, recent=0, insert=None):
    insert = insert or AsyncMock(return_value=7)
    with patch.object(collection, "owns_turn", AsyncMock(return_value=owns)), \
            patch.object(collection.repo, "count_recent_reports", AsyncMock(return_value=recent)), \
            patch.object(collection.repo, "insert_developer_report", insert):
        return client().post("/api/v1/developer-reports", json=body), insert


def test_report_is_stored_with_own_turn():
    r, insert = _post({"kind": "bug", "message": "  비교 질문에서 제품을 못 찾아요  ", "turn_id": "t1"})
    assert r.status_code == 201 and r.json() == {"id": 7}
    assert r.headers["cache-control"] == "no-store"
    kwargs = insert.await_args.kwargs
    assert kwargs["subject_id"] == SUBJECT and kwargs["kind"] == "bug"
    assert kwargs["message"] == "비교 질문에서 제품을 못 찾아요" and kwargs["turn_id"] == "t1"


def test_turn_of_another_session_is_dropped():
    r, insert = _post({"kind": "improvement", "message": "다크모드가 있으면 좋겠어요", "turn_id": "other"}, owns=False)
    assert r.status_code == 201 and insert.await_args.kwargs["turn_id"] is None


def test_private_input_and_unknown_kind_rejected():
    r, insert = _post({"kind": "bug", "message": "연락처 010-1234-5678로 답 주세요"})
    assert r.status_code == 422 and "개인정보" in r.json()["detail"]
    insert.assert_not_awaited()
    r, insert = _post({"kind": "spam", "message": "x"})
    assert r.status_code == 422
    insert.assert_not_awaited()


def test_rate_limited():
    r, insert = _post({"kind": "other", "message": "또 보내요"}, recent=consent_policy.REPORT_RATE_LIMIT)
    assert r.status_code == 429
    insert.assert_not_awaited()


def test_store_failure_is_503():
    r, _ = _post({"kind": "bug", "message": "오류"}, insert=AsyncMock(side_effect=RuntimeError("db")))
    assert r.status_code == 503


def test_policy_exposes_report_kinds():
    d = TestClient(app).get("/api/v1/data-policy").json()
    assert d["report_kinds"]["bug"] == "오류 신고" and d["report_max_chars"] == 1000


def test_sidebar_has_report_entry():
    html = TestClient(app).get("/").text
    assert 'id="open-report"' in html and "개발자에게 의견 보내기" in html
    assert 'id="report-dialog"' in html and "/api/v1/developer-reports" in html
