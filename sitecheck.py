"""SiteCheck - a small website health checker for the Minibyte site."""

import argparse
import csv
import json
import math
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from rich.console import Console
from rich.markup import escape
from rich.table import Table

USER_AGENT = "SiteCheck/1.0 (Minibyte website health checker)"
SLOW_SECONDS = 1.5
MAX_BYTES = 1_000_000
MAX_REDIRECT_HOPS = 1  # more than this many redirects in a row = a redirect chain
REDIRECT_LIMIT = 10  # stop following redirects after this many hops
REDIRECT_CODES = (301, 302, 303, 307, 308)
SECURITY_HEADERS = ["Content-Security-Policy", "X-Content-Type-Options"]
ALLOWED_HOSTS = {"minibyte.owaiskaifi.workers.dev", "127.0.0.1", "localhost"}
SLOWEST_PAGES_SHOWN = 20

console = Console()


@dataclass
class Issue:
    url: str
    category: str  # link, seo, accessibility, performance, security
    severity: str  # error or warning
    message: str


@dataclass
class PageRecord:
    """One row per crawled page, written to the report."""
    url: str
    status_code: Optional[int]  # None if the request failed completely
    response_time: Optional[float]  # seconds
    redirects: int  # number of redirects followed to reach the page
    final_url: str


@dataclass
class Fetched:
    """The result of fetching one URL (after following any safe redirects)."""
    url: str  # final URL
    status_code: int
    headers: httpx.Headers
    content: bytes = b""
    truncated: bool = False  # True if we stopped downloading at MAX_BYTES
    hops: int = 0
    elapsed: float = 0.0
    problem: Optional[str] = None  # redirect problem, if any
    problem_severity: str = "error"

    @property
    def text(self):
        content_type = self.headers.get("content-type", "")
        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=")[-1].split(";")[0].strip().strip("\"'") or "utf-8"
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


# ---------- helpers ----------

def describe_error(exc):
    return f"{type(exc).__name__}: {exc}".rstrip(": ")


def normalize_url(url):
    """Remove #fragments and trailing slashes so the same page is not visited twice."""
    url, _ = urldefrag(url)
    parts = urlparse(url)
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return parts._replace(path=path).geturl()


def _request_once(client, method, url, read_body, max_bytes):
    """One request, no redirect following. The body is streamed and the download
    is stopped as soon as it passes max_bytes."""
    with client.stream(method, url) as response:
        status = response.status_code
        headers = response.headers
        chunks, size, truncated = [], 0, False
        if read_body and method != "HEAD" and status not in REDIRECT_CODES:
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    truncated = True
                    break  # leaving the with-block closes the connection
    return status, headers, b"".join(chunks), truncated


def fetch(client, url, method="GET", allowed_host=None, read_body=True, max_bytes=None):
    """Fetch a URL and follow redirects by hand so we can count them and keep them safe.

    - allowed_host: if set, a redirect to any other host is NOT followed.
    - Redirect loops, missing Location headers and more than REDIRECT_LIMIT hops
      stop the chain and are reported in Fetched.problem.
    - HTTP errors (httpx.HTTPError) are raised to the caller.
    """
    if max_bytes is None:
        max_bytes = MAX_BYTES
    start = time.perf_counter()
    current = url
    seen = {url}
    hops = 0
    problem = None
    severity = "error"

    while True:
        status, headers, body, truncated = _request_once(client, method, current, read_body, max_bytes)
        if status in REDIRECT_CODES and headers.get("location"):
            target = urldefrag(urljoin(current, headers["location"]))[0]
            if urlparse(target).scheme not in ("http", "https"):
                problem = f"redirect to unsupported URL {target}"
            elif allowed_host and urlparse(target).hostname != allowed_host:
                problem = f"redirect leaves {allowed_host} -> {target} (not followed)"
                severity = "warning"
            elif target in seen:
                problem = f"redirect loop at {target}"
            elif hops >= REDIRECT_LIMIT:
                problem = f"more than {REDIRECT_LIMIT} redirects (stopped following)"
            else:
                seen.add(target)
                hops += 1
                current = target
                continue
        elif 300 <= status < 400 and status != 304:
            problem = f"HTTP {status} redirect without a Location header"
        break

    return Fetched(
        url=current, status_code=status, headers=headers, content=body,
        truncated=truncated, hops=hops, elapsed=time.perf_counter() - start,
        problem=problem, problem_severity=severity,
    )


def get_title_and_description(soup):
    title = soup.title.get_text(strip=True) if soup.title else ""
    tag = soup.find("meta", attrs={"name": "description"})
    description = (tag.get("content") or "").strip() if tag else ""
    return title, description


# ---------- checks ----------

def link_issues(url, result, source=None):
    """Status code and redirect problems for one fetched URL."""
    issues = []
    where = f" (linked from {source})" if source else ""
    code = result.status_code
    if code >= 500:
        issues.append(Issue(url, "link", "error", f"HTTP {code} server error{where}"))
    elif code == 404:
        issues.append(Issue(url, "link", "error", f"HTTP 404 broken link{where}"))
    elif code >= 400:
        issues.append(Issue(url, "link", "error", f"HTTP {code} client error{where}"))
    if result.problem:
        issues.append(Issue(url, "link", result.problem_severity, f"{result.problem}{where}"))
    if result.hops > MAX_REDIRECT_HOPS:
        issues.append(Issue(url, "link", "warning",
                            f"redirect chain of {result.hops} hops, ends at {result.url}{where}"))
    return issues


def check_page(url, response, elapsed, truncated=False):
    """Quality, performance and security checks for one HTML page.

    `response` only needs .headers, .content and .text, so both an httpx.Response
    and our Fetched object work.
    """
    issues = []

    def add(category, severity, message):
        issues.append(Issue(url, category, severity, message))

    if elapsed > SLOW_SECONDS:
        add("performance", "warning", f"slow page: {elapsed:.2f}s (limit {SLOW_SECONDS}s)")

    size = len(response.content)
    if truncated:
        add("performance", "warning",
            f"large page: over {MAX_BYTES} bytes (download stopped at the limit)")
    elif size > MAX_BYTES:
        add("performance", "warning", f"large page: {size} bytes (limit {MAX_BYTES})")

    for header in SECURITY_HEADERS:
        if header not in response.headers:
            add("security", "warning", f"missing security header: {header}")

    if truncated:
        return issues  # only part of the page was downloaded, so content checks would be unreliable

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
    """Check an external link once. HEAD first; fall back to GET when HEAD is
    unsupported, errors out, or gives an error status. The body is never downloaded."""
    result = None
    try:
        result = fetch(client, url, method="HEAD", read_body=False)
    except httpx.HTTPError:
        result = None

    if result is None or result.status_code >= 400 or result.problem:
        try:
            result = fetch(client, url, method="GET", read_body=False)
        except httpx.HTTPError as exc:
            return [Issue(url, "link", "error",
                          f"external link failed: {describe_error(exc)} (linked from {source})")]
    return link_issues(url, result, source)


# ---------- crawler ----------

def crawl(start_url, delay=0.5, max_pages=200):
    """Crawl internal pages one by one. Returns (issues, page_records)."""
    start_url = normalize_url(start_url)
    host = urlparse(start_url).hostname
    queue = deque([start_url])
    queued = {start_url}
    sources = {}
    external = {}
    titles, descriptions = {}, {}
    issues = []
    pages = []

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=10.0) as client:
        while queue and len(pages) < max_pages:
            url = queue.popleft()
            if pages:
                time.sleep(delay)

            try:
                result = fetch(client, url, allowed_host=host)
            except httpx.HTTPError as exc:
                issues.append(Issue(url, "link", "error", f"request failed: {describe_error(exc)}"))
                pages.append(PageRecord(url, None, None, 0, url))
                continue

            pages.append(PageRecord(url, result.status_code, round(result.elapsed, 3),
                                    result.hops, result.url))
            queued.add(normalize_url(result.url))  # do not crawl the redirect target again
            issues.extend(link_issues(url, result, sources.get(url)))

            is_html = "text/html" in result.headers.get("content-type", "")
            if result.status_code != 200 or not is_html:
                continue

            issues.extend(check_page(url, result, result.elapsed, truncated=result.truncated))

            soup = BeautifulSoup(result.text, "html.parser")
            title, description = get_title_and_description(soup)
            if title:
                titles.setdefault(title, []).append(url)
            if description:
                descriptions.setdefault(description, []).append(url)

            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if href.startswith(("mailto:", "tel:", "javascript:")):
                    continue
                link = normalize_url(urljoin(result.url, href))
                parts = urlparse(link)
                if parts.scheme not in ("http", "https"):
                    continue
                if parts.hostname == host:
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
    console.print(f"Pages crawled: {len(pages)}   Issues: {len(issues)} "
                  f"([red]{errors} errors[/red], [yellow]{warnings} warnings[/yellow])\n")

    page_table = Table(title="Slowest pages" if len(pages) > SLOWEST_PAGES_SHOWN else "Pages crawled")
    page_table.add_column("URL", overflow="fold")
    page_table.add_column("Status", justify="right")
    page_table.add_column("Time (s)", justify="right")
    page_table.add_column("Redirects", justify="right")
    by_time = sorted(pages, key=lambda p: p.response_time or 0, reverse=True)
    for p in by_time[:SLOWEST_PAGES_SHOWN]:
        page_table.add_row(
            escape(p.url),
            str(p.status_code) if p.status_code is not None else "failed",
            f"{p.response_time:.2f}" if p.response_time is not None else "-",
            str(p.redirects),
        )
    console.print(page_table)
    if len(pages) > SLOWEST_PAGES_SHOWN:
        console.print(f"(showing {SLOWEST_PAGES_SHOWN} of {len(pages)}; all pages are in the report file)")

    if not issues:
        console.print("\n[green]No issues found.[/green]")
        return

    table = Table(title="\nIssues by category")
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
    """JSON: one file with the summary, every page and every issue.
    CSV: the issues in <path>, and the per-page data in <name>-pages.csv."""
    if fmt == "json":
        data = {
            "site": start_url,
            "pages_crawled": len(pages),
            "total_issues": len(issues),
            "pages": [asdict(p) for p in pages],
            "issues": [asdict(i) for i in issues],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return [path]

    pages_path = str(Path(path).with_name(Path(path).stem + "-pages.csv"))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "category", "severity", "message"])
        writer.writeheader()
        writer.writerows(asdict(i) for i in issues)
    with open(pages_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "status_code", "response_time", "redirects", "final_url"])
        writer.writeheader()
        writer.writerows(asdict(p) for p in pages)
    return [path, pages_path]


# ---------- command line ----------

def non_negative_float(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number")
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return number


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number")
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {value}")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sitecheck", description="Check a website's health.")
    parser.add_argument("url", help="start URL, e.g. https://minibyte.owaiskaifi.workers.dev")
    parser.add_argument("--delay", type=non_negative_float, default=0.5,
                        help="seconds between requests (0 or more, default 0.5)")
    parser.add_argument("--max-pages", type=positive_int, default=200,
                        help="safety limit on pages crawled (1 or more, default 200)")
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
    written = write_report(output, args.format, args.url, pages, issues)
    console.print("\nReport saved to " + ", ".join(escape(p) for p in written))
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
