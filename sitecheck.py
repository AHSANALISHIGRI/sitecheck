"""SiteCheck - a small website health checker for the Minibyte site."""

import argparse
import csv
import json
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from rich.console import Console
from rich.markup import escape
from rich.table import Table

USER_AGENT = "SiteCheck/1.0 (Minibyte website health checker)"
SLOW_SECONDS = 1.5
MAX_BYTES = 1_000_000
MAX_REDIRECT_HOPS = 1  # more than this many redirects in a row = a chain
SECURITY_HEADERS = ["Content-Security-Policy", "X-Content-Type-Options"]
ALLOWED_HOSTS = {"minibyte.owaiskaifi.workers.dev", "127.0.0.1", "localhost"}

console = Console()


@dataclass
class Issue:
    url: str
    category: str  # link, seo, accessibility, performance, security
    severity: str  # error or warning
    message: str


# ---------- helpers ----------

def normalize_url(url):
    """Remove #fragments and trailing slashes so the same page is not visited twice."""
    url, _ = urldefrag(url)
    parts = urlparse(url)
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return parts._replace(path=path).geturl()


def fetch(client, url, method="GET", max_hops=10):
    """Request a URL and follow redirects by hand so we can count the hops."""
    start = time.perf_counter()
    hops = 0
    response = client.request(method, url)
    while response.is_redirect and hops < max_hops:
        url = urljoin(str(response.url), response.headers["location"])
        response = client.request(method, url)
        hops += 1
    return response, hops, time.perf_counter() - start


def get_title_and_description(soup):
    title = soup.title.get_text(strip=True) if soup.title else ""
    tag = soup.find("meta", attrs={"name": "description"})
    description = (tag.get("content") or "").strip() if tag else ""
    return title, description


# ---------- checks ----------

def link_issues(url, response, hops, source=None):
    """Status code and redirect problems for one URL."""
    issues = []
    where = f" (linked from {source})" if source else ""
    code = response.status_code
    if code >= 500:
        issues.append(Issue(url, "link", "error", f"HTTP {code} server error{where}"))
    elif code == 404:
        issues.append(Issue(url, "link", "error", f"HTTP 404 broken link{where}"))
    elif code >= 400:
        issues.append(Issue(url, "link", "error", f"HTTP {code} client error{where}"))
    if hops > MAX_REDIRECT_HOPS:
        issues.append(Issue(url, "link", "warning", f"redirect chain of {hops} hops{where}"))
    return issues


def check_page(url, response, elapsed):
    """Quality, performance and security checks for one HTML page."""
    issues = []

    def add(category, severity, message):
        issues.append(Issue(url, category, severity, message))

    if elapsed > SLOW_SECONDS:
        add("performance", "warning", f"slow page: {elapsed:.2f}s (limit {SLOW_SECONDS}s)")
    size = len(response.content)
    if size > MAX_BYTES:
        add("performance", "warning", f"large page: {size} bytes (limit {MAX_BYTES})")

    for header in SECURITY_HEADERS:
        if header not in response.headers:
            add("security", "warning", f"missing security header: {header}")

    soup = BeautifulSoup(response.text, "html.parser")
    title, description = get_title_and_description(soup)
    if not title:
        add("seo", "error", "missing <title>")
    if not description:
        add("seo", "error", "missing meta description")

    h1_count = len(soup.find_all("h1"))
    if h1_count != 1:
        add("seo", "warning", f"expected exactly 1 <h1>, found {h1_count}")

    # Rule: a missing alt attribute is an issue. alt="" is allowed (decorative image).
    for img in soup.find_all("img"):
        if img.get("alt") is None:
            add("accessibility", "warning", f"image without alt attribute: {img.get('src', '(no src)')}")
    return issues


def check_external(client, url, source):
    """HEAD first, fall back to GET if HEAD fails."""
    try:
        response, hops, _ = fetch(client, url, method="HEAD")
        if response.status_code >= 400:
            response, hops, _ = fetch(client, url, method="GET")
    except httpx.HTTPError:
        try:
            response, hops, _ = fetch(client, url, method="GET")
        except httpx.HTTPError as exc:
            return [Issue(url, "link", "error", f"external link failed: {exc} (linked from {source})")]
    return link_issues(url, response, hops, source)


# ---------- crawler ----------

def crawl(start_url, delay=0.5, max_pages=200):
    """Crawl internal pages one by one. Returns (issues, pages_crawled)."""
    start_url = normalize_url(start_url)
    host = urlparse(start_url).netloc
    queue = deque([start_url])
    queued = {start_url}
    sources = {}
    external = {}
    titles, descriptions = {}, {}
    issues = []
    pages = 0

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=10.0) as client:
        while queue and pages < max_pages:
            url = queue.popleft()
            if pages > 0:
                time.sleep(delay)
            pages += 1

            try:
                response, hops, elapsed = fetch(client, url)
            except httpx.HTTPError as exc:
                issues.append(Issue(url, "link", "error", f"request failed: {exc}"))
                continue

            issues.extend(link_issues(url, response, hops, sources.get(url)))
            is_html = "text/html" in response.headers.get("content-type", "")
            if response.status_code != 200 or not is_html:
                continue

            issues.extend(check_page(url, response, elapsed))

            soup = BeautifulSoup(response.text, "html.parser")
            title, description = get_title_and_description(soup)
            if title:
                titles.setdefault(title, []).append(url)
            if description:
                descriptions.setdefault(description, []).append(url)

            base = str(response.url)
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if href.startswith(("mailto:", "tel:", "javascript:")):
                    continue
                link = normalize_url(urljoin(base, href))
                parts = urlparse(link)
                if parts.scheme not in ("http", "https"):
                    continue
                if parts.netloc == host:
                    if link not in queued:
                        queued.add(link)
                        queue.append(link)
                        sources[link] = url
                else:
                    external.setdefault(link, url)

        # external links: checked once each, never crawled
        for link, source in external.items():
            time.sleep(delay)
            issues.extend(check_external(client, link, source))

    for label, mapping in (("title", titles), ("meta description", descriptions)):
        for text, urls in mapping.items():
            if len(urls) > 1:
                for u in urls:
                    issues.append(Issue(u, "seo", "warning",
                                        f"duplicate {label} '{text}' (shared by {len(urls)} pages)"))
    return issues, pages


# ---------- reporting ----------

def print_summary(start_url, pages, issues):
    errors = sum(1 for i in issues if i.severity == "error")
    warnings = len(issues) - errors
    console.print(f"\n[bold]SiteCheck report for {escape(start_url)}[/bold]")
    console.print(f"Pages crawled: {pages}   Issues: {len(issues)} "
                  f"([red]{errors} errors[/red], [yellow]{warnings} warnings[/yellow])\n")
    if not issues:
        console.print("[green]No issues found.[/green]")
        return

    table = Table(title="Issues by category")
    table.add_column("Category")
    table.add_column("Errors", justify="right")
    table.add_column("Warnings", justify="right")
    for category in sorted({i.category for i in issues}):
        e = sum(1 for i in issues if i.category == category and i.severity == "error")
        w = sum(1 for i in issues if i.category == category and i.severity == "warning")
        table.add_row(category, str(e), str(w))
    console.print(table)

    console.print("\n[bold]Details[/bold]")
    for issue in issues:
        color = "red" if issue.severity == "error" else "yellow"
        console.print(f"[{color}]{issue.severity.upper():7}[/{color}] "
                      f"{issue.category:13} {escape(issue.url)}\n          {escape(issue.message)}")


def write_report(path, fmt, start_url, pages, issues):
    if fmt == "json":
        data = {
            "site": start_url,
            "pages_crawled": pages,
            "total_issues": len(issues),
            "issues": [asdict(i) for i in issues],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    else:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["url", "category", "severity", "message"])
            writer.writeheader()
            writer.writerows(asdict(i) for i in issues)


# ---------- command line ----------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="sitecheck", description="Check a website's health.")
    parser.add_argument("url", help="start URL, e.g. https://minibyte.owaiskaifi.workers.dev")
    parser.add_argument("--delay", type=float, default=0.5, help="seconds between requests")
    parser.add_argument("--max-pages", type=int, default=200, help="safety limit on pages crawled")
    parser.add_argument("--format", choices=["json", "csv"], default="json")
    parser.add_argument("--output", help="report file name")
    args = parser.parse_args(argv)

    parts = urlparse(args.url)
    if parts.scheme not in ("http", "https") or parts.hostname not in ALLOWED_HOSTS:
        print("Error: SiteCheck only scans the Minibyte site (or localhost for tests).")
        return 2

    issues, pages = crawl(args.url, delay=args.delay, max_pages=args.max_pages)
    output = args.output or f"sitecheck-report.{args.format}"
    print_summary(args.url, pages, issues)
    write_report(output, args.format, args.url, pages, issues)
    console.print(f"\nReport saved to {escape(output)}")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())