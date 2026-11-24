# SiteCheck

SiteCheck is a small command-line tool that checks the Minibyte website for broken links, missing page content, performance problems, and missing security headers. It crawls the site, checks each page, and produces a report of anything that needs fixing.

It only scans the Minibyte site (`minibyte.owaiskaifi.workers.dev`). For testing, it also accepts `127.0.0.1` and `localhost`. Any other domain is refused.

## Requirements

- Python 3.10 or newer
- Dependencies: `httpx`, `beautifulsoup4`, `pytest`, `rich`

## Installation

### Option A: venv

```bash
python -m venv .venv
# Windows (PowerShell): .venv\Scripts\Activate.ps1
# Mac/Linux:            source .venv/bin/activate
python -m pip install -r requirements.txt
```

### Option B: uv

```bash
uv venv
# Windows (PowerShell): .venv\Scripts\Activate.ps1
# Mac/Linux:            source .venv/bin/activate
uv pip install -r requirements.txt
```

## Usage

```bash
python sitecheck.py https://minibyte.owaiskaifi.workers.dev
```

Options:

| Option | Default | Description |
|---|---|---|
| `--delay` | 0.5 | Seconds to wait between requests |
| `--max-pages` | 200 | Safety limit on the number of pages crawled |
| `--format` | json | Report format: `json` or `csv` |
| `--output` | `sitecheck-report.<format>` | Report file name |

Example:

```bash
python sitecheck.py https://minibyte.owaiskaifi.workers.dev --format csv --output report.csv
```

## What it checks

| Category | Check | Severity |
|---|---|---|
| Links | Broken internal or external links (404) | error |
| Links | Server errors (5xx) and other client errors (4xx) | error |
| Links | Redirect chains of more than 1 hop | warning |
| SEO | Missing `<title>` | error |
| SEO | Missing meta description | error |
| SEO | Duplicate titles or meta descriptions across pages | warning |
| SEO | Page does not have exactly one `<h1>` | warning |
| Accessibility | `<img>` with no `alt` attribute (`alt=""` is allowed for decorative images) | warning |
| Performance | Page takes longer than 1.5 seconds to load | warning |
| Performance | Page larger than 1 MB | warning |
| Security | Missing `Content-Security-Policy` header | warning |
| Security | Missing `X-Content-Type-Options` header | warning |

## How it crawls

- Starts at the given URL and follows internal links (same domain).
- Each page is visited once. Fragments (`#...`) and trailing slashes are ignored.
- External links are checked once with a `HEAD` request, falling back to `GET` if needed. They are never crawled.
- Requests are sequential, with a delay between them and a clear User-Agent (`SiteCheck/1.0`).

## Output

- A summary in the terminal: pages crawled, total issues, issues by category, and a detailed list.
- A report file (JSON or CSV) with every issue: `url`, `category`, `severity`, `message`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | No issues found |
| 1 | One or more issues found (useful for CI/CD) |
| 2 | Invalid input, such as a domain that is not allowed |

## Running the tests

```bash
python -m pytest -v
```

The tests use small HTML pages in `tests/fixtures/`, served by a local test server, so they never touch the live website.

## Rules

- Only scan the Minibyte website.
- Only public pages. No logins, staff-only pages, or secrets.
- Do not run the crawler repeatedly or in a tight loop.