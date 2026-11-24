SiteCheck

SiteCheck is a small command-line tool that checks the Minibyte website for broken links, missing page content, performance problems, and missing security headers. It crawls the site, checks each page, and produces a report of anything that needs fixing.

It only scans the Minibyte site (minibyte.owaiskaifi.workers.dev). For testing, it also accepts 127.0.0.1 and localhost. Any other domain is refused.

Requirements
Python 3.10 or newer
Dependencies: httpx, beautifulsoup4, pytest, rich
Installation
Option A: venv
bash
python -m venv .venv
# Windows (PowerShell): .venv\Scripts\Activate.ps1
# Mac/Linux:            source .venv/bin/activate
python -m pip install -r requirements.txt
Option B: uv
bash
uv venv
# Windows (PowerShell): .venv\Scripts\Activate.ps1
# Mac/Linux:            source .venv/bin/activate
uv pip install -r requirements.txt
Usage
bash
python sitecheck.py https://minibyte.owaiskaifi.workers.dev

Options:

Option	Default	Description
--delay	0.5	Seconds to wait between requests (0 or more)
--max-pages	200	Safety limit on the number of pages crawled (1 or more)
--format	json	Report format: json or csv
--output	sitecheck-report.<format>	Report file name

Invalid values (for example a negative delay or --max-pages 0) are rejected with an error and exit code 2.

Example:

bash
python sitecheck.py https://minibyte.owaiskaifi.workers.dev --format csv --output report.csv
What it checks
Category	Check	Severity
Links	Broken internal or external links (404)	error
Links	Server errors (5xx) and other client errors (4xx)	error
Links	Redirect chains of more than 1 hop	warning
Links	Redirect loops, more than 10 redirects, redirects with no Location header	error
Links	Internal redirect that leaves the Minibyte domain (not followed)	warning
SEO	Missing <title>	error
SEO	Missing meta description	error
SEO	Duplicate titles or meta descriptions across pages	warning
SEO	Page does not have exactly one <h1>	warning
Accessibility	<img> with no alt attribute (alt="" is allowed for decorative images)	warning
Performance	Page takes longer than 1.5 seconds to load	warning
Performance	Page larger than 1 MB (the download stops at the limit)	warning
Security	Missing Content-Security-Policy header	warning
Security	Missing X-Content-Type-Options header	warning

For a page larger than 1 MB only part of the page is downloaded, so its title, description, <h1> and image checks are skipped.

How it crawls
Starts at the given URL and follows internal links (same domain).
Each page is visited once. Fragments (#...) and trailing slashes are ignored.
Redirects are followed by hand. An internal redirect to another domain is never followed.
External links are checked once, never crawled. SiteCheck sends HEAD first and falls back to GET if HEAD is unsupported, fails, or returns an error. The page body of an external link is never downloaded.
Requests are sequential, with a delay between them and a clear User-Agent (SiteCheck/1.0).
Output
A summary in the terminal: pages crawled with status, response time and redirect count (the 20 slowest if there are more), issues by category, and a detailed list.
JSON report: site, pages_crawled, total_issues, pages (url, status_code, response_time, redirects, final_url) and issues (url, category, severity, message).
CSV report: the issues in the output file, plus the per-page data in <name>-pages.csv.
Exit codes
Code	Meaning
0	No issues found
1	One or more issues found (useful for CI/CD)
2	Invalid input, such as a domain that is not allowed or a bad option value
Running the tests
bash
python -m pytest -v

The tests use small HTML pages in tests/fixtures/, served by a local test server, and fake HTTP responses for redirects, large pages and external links, so they never touch the live website.

Rules
Only scan the Minibyte website.
Only public pages. No logins, staff-only pages, or secrets.
Do not run the crawler repeatedly or in a tight loop.
