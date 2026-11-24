import subprocess
import sys
from pathlib import Path

import httpx

import sitecheck
from sitecheck import check_page, crawl, main

ROOT = Path(__file__).parent.parent


def messages(issues):
    return " | ".join(i.message.lower() for i in issues)


def page_issues(base_url, path, elapsed=0.1):
    url = base_url + path
    return check_page(url, httpx.get(url), elapsed)


def test_missing_title_and_description(base_url):
    text = messages(page_issues(base_url, "/bad.html"))
    assert "missing <title>" in text
    assert "missing meta description" in text


def test_two_h1_flagged(base_url):
    assert "found 2" in messages(page_issues(base_url, "/bad.html"))


def test_image_without_alt(base_url):
    assert "image without alt" in messages(page_issues(base_url, "/bad.html"))


def test_clean_page_has_no_issues(base_url):
    assert page_issues(base_url, "/secure.html") == []


def test_missing_security_headers(base_url):
    text = messages(page_issues(base_url, "/bad.html"))
    assert "content-security-policy" in text
    assert "x-content-type-options" in text


def test_slow_page_flagged(base_url):
    issues = page_issues(base_url, "/secure.html", elapsed=2.0)  # fake the timing
    assert "slow page" in messages(issues)


def test_large_page_flagged(base_url, monkeypatch):
    monkeypatch.setattr(sitecheck, "MAX_BYTES", 10)  # tiny limit for the test
    issues = page_issues(base_url, "/secure.html")
    assert "large page" in messages(issues)


def test_crawl_finds_broken_link(base_url):
    issues, pages = crawl(base_url, delay=0)
    assert pages == 4
    assert any("404" in i.message and i.url.endswith("/missing.html") for i in issues)


def test_each_page_visited_once(base_url):
    issues, pages = crawl(base_url, delay=0)
    assert pages == 4  # home, bad, secure, missing (no repeats)


def test_other_domains_are_refused():
    assert main(["https://example.com"]) == 2


def test_exit_code_1_when_issues_found(base_url, tmp_path):
    result = subprocess.run(
        [sys.executable, "sitecheck.py", base_url, "--delay", "0",
         "--output", str(tmp_path / "report.json")],
        cwd=ROOT, capture_output=True,
    )
    assert result.returncode == 1
    assert (tmp_path / "report.json").exists()