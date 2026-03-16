"""
测试 E3：SEO 基础要素
- E3-1 /robots.txt 返回 200，包含 Sitemap 声明和 Disallow /api/
- E3-2 /sitemap.xml 返回 200，包含 fixepub.com URL，Content-Type 为 XML
- E3-3 index.html 包含 <title>、description meta、og:title、canonical
- E3-4 index.html 包含 JSON-LD 结构化数据
- E3-5 index.html 包含 FAQ section（SEO 可见内容）
"""
import os
import sys
import tempfile

_tmp_jobs_db = tempfile.mktemp(suffix="_jobs.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_jobs_db}"
os.environ["SKIP_PAYMENT_CHECK"] = "1"

sys.path.insert(0, os.path.dirname(__file__))

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_e3_1_robots_txt():
    r = client.get("/robots.txt")
    assert r.status_code == 200, f"robots.txt 应返回 200，实际 {r.status_code}"
    body = r.text
    assert "Sitemap:" in body, "robots.txt 应包含 Sitemap 声明"
    assert "Disallow: /api/" in body, "robots.txt 应屏蔽 /api/ 路径"
    assert "User-agent: *" in body


def test_e3_2_sitemap_xml():
    r = client.get("/sitemap.xml")
    assert r.status_code == 200, f"sitemap.xml 应返回 200，实际 {r.status_code}"
    assert "xml" in r.headers.get("content-type", ""), "Content-Type 应为 XML"
    body = r.text
    assert "fixepub.com" in body, "sitemap 应包含网站 URL"
    assert "<urlset" in body, "应为合法 sitemap XML"
    assert "<loc>" in body


def test_e3_3_index_html_meta_tags():
    from pathlib import Path
    html_path = Path(__file__).parent.parent / "frontend" / "index.html"
    assert html_path.exists(), "index.html 不存在"
    html = html_path.read_text(encoding="utf-8")

    assert "<title>" in html and "EPUB" in html, "title 应包含 EPUB"
    assert 'name="description"' in html, "缺少 description meta"
    assert 'property="og:title"' in html, "缺少 og:title"
    assert 'property="og:description"' in html, "缺少 og:description"
    assert 'rel="canonical"' in html, "缺少 canonical 链接"
    assert "fixepub.com" in html, "canonical 应包含域名"


def test_e3_4_json_ld():
    from pathlib import Path
    html = (Path(__file__).parent.parent / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'application/ld+json' in html, "缺少 JSON-LD 结构化数据"
    assert "WebApplication" in html, "JSON-LD 类型应为 WebApplication"
    assert "featureList" in html, "JSON-LD 应包含 featureList"


def test_e3_5_faq_section():
    from pathlib import Path
    html = (Path(__file__).parent.parent / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "faq-item" in html, "缺少 FAQ section"
    assert "<details" in html, "FAQ 应使用 <details> 元素"
    assert "繁体转简体" in html or "繁简" in html, "FAQ 应包含繁简转换关键词"
    assert "Kindle" in html, "FAQ 应包含 Kindle 关键词"


# ── 运行 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [
        test_e3_1_robots_txt,
        test_e3_2_sitemap_xml,
        test_e3_3_index_html_meta_tags,
        test_e3_4_json_ld,
        test_e3_5_faq_section,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__} [异常]: {e}")
            failed += 1

    print(f"\n{'─'*52}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'─'*52}")

    import os as _os
    try:
        _os.unlink(_tmp_jobs_db)
    except Exception:
        pass
    sys.exit(1 if failed else 0)
