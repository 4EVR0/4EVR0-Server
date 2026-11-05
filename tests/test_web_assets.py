"""새 화면이 브라우저에 남은 이전 스타일을 재사용하지 않도록 한다."""
import re
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


def stylesheet(html):
    return re.search(r'href="(/static/olive[.]css[^\"]*)"', html).group(1)


def test_home_revalidates_and_versioned_stylesheet_is_served():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    url = stylesheet(response.text)
    assert "?v=" in url
    css = client.get(url)
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]


def test_stylesheet_url_changes_when_css_changes(tmp_path):
    (tmp_path / "index.html").write_text('<link rel="stylesheet" href="/static/olive.css">')
    css = tmp_path / "olive.css"
    client = TestClient(app)
    with patch("app.main._STATIC_DIR", tmp_path):
        css.write_text("body { color: olive; }")
        first = stylesheet(client.get("/").text)
        css.write_text("body { color: green; }")
        second = stylesheet(client.get("/").text)
    assert first != second
