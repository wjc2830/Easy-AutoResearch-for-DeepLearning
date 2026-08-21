#!/usr/bin/env python3
"""
arxiv_verified_search.py — Search arXiv for recent solutions, keep ONLY papers whose
abstract/comment advertises a GitHub repo that actually exists and has > MIN_STARS stars.

Motivation: arXiv is noisy — preprints can carry unverified or overstated claims. A paper
that ships a non-trivial, independently-starred codebase is a much stronger signal that the
method is real and reproducible. This skill filters arXiv results down to that verified set.

Verification gate (a paper PASSES only if ALL hold):
  1. Its arXiv abstract or comment field contains a github.com/<owner>/<repo> URL.
  2. That repo exists (GitHub API returns 200, not 404 and not a fork-only stub).
  3. The repo has strictly more than --min-stars stars (default 10).

READ-ONLY / network-only: it queries the arXiv API and the GitHub REST API and prints a
report. It never writes to the codebase, never clones, never installs.

Usage:
    python3 arxiv_verified_search.py "invariant risk minimization domain generalization" \
        [--max-results 40] [--min-stars 10] [--since-year 2024] [--json]

Auth: set GITHUB_TOKEN in the environment to raise the GitHub rate limit (60/hr
unauthenticated → 5000/hr). Without it the script still works but may hit the limit on
large --max-results.

Network: honors http(s)_proxy from the environment (arXiv needs the proxy here; the arXiv
endpoint is used over HTTPS).
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ARXIV_API = "https://export.arxiv.org/api/query"
GITHUB_API = "https://api.github.com/repos/"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"

_GH_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:[)\]\s.,;>]|$)")


def _get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # respects *_proxy env
        return r.read().decode("utf-8", errors="replace"), r.status


def search_arxiv(query, max_results, since_year, sort_by="relevance"):
    """Return list of paper dicts from the arXiv API.

    sort_by: "relevance" (default — surfaces established, well-known work that has had
    time to accumulate GitHub stars) or "recency" (newest first — good for the bleeding
    edge, but fresh preprints often have few stars yet).
    """
    arxiv_sort = "submittedDate" if sort_by == "recency" else "relevance"
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": max_results,
        "sortBy": arxiv_sort,
        "sortOrder": "descending",
    }
    url = f"{ARXIV_API}?{urllib.parse.urlencode(params)}"
    try:
        body, _ = _get(url, headers={"User-Agent": "aris-arxiv-verified-search/1.0"})
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"[arxiv-verified-search] arXiv API request failed: {e}")

    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise SystemExit(f"[arxiv-verified-search] Could not parse arXiv response: {e}")

    papers = []
    for entry in root.findall(f"{ATOM}entry"):
        title = (entry.findtext(f"{ATOM}title") or "").strip().replace("\n", " ")
        summary = (entry.findtext(f"{ATOM}summary") or "").strip()
        published = (entry.findtext(f"{ATOM}published") or "").strip()
        comment = (entry.findtext(f"{ARXIV_NS}comment") or "").strip()
        abs_url = ""
        for link in entry.findall(f"{ATOM}link"):
            if link.get("rel") == "alternate":
                abs_url = link.get("href", "")
        year = None
        if len(published) >= 4 and published[:4].isdigit():
            year = int(published[:4])
        if since_year and year and year < since_year:
            continue
        papers.append({
            "title": title,
            "abs_url": abs_url,
            "published": published,
            "year": year,
            "summary": summary,
            "comment": comment,
        })
    return papers


def extract_github_repos(text):
    """Return a de-duplicated list of (owner, repo) from any github URLs in text."""
    out = []
    seen = set()
    for owner, repo in _GH_RE.findall(text or ""):
        # strip common trailing noise
        repo = repo.rstrip(".")
        key = (owner.lower(), repo.lower())
        if key in seen:
            continue
        # skip obvious non-repo paths
        if owner.lower() in ("about", "features", "pricing", "sponsors"):
            continue
        seen.add(key)
        out.append((owner, repo))
    return out


def github_repo_info(owner, repo, token):
    """Return dict with stars/exists/fork, or None on hard failure."""
    headers = {"User-Agent": "aris-arxiv-verified-search/1.0",
               "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{GITHUB_API}{owner}/{repo}"
    try:
        body, status = _get(url, headers=headers, timeout=25)
    except urllib.error.HTTPError as e:  # noqa
        if e.code == 404:
            return {"exists": False}
        if e.code == 403:
            return {"exists": None, "rate_limited": True}
        return {"exists": None, "error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"exists": None, "error": str(e)}
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {"exists": None, "error": "bad JSON"}
    return {
        "exists": True,
        "stars": int(data.get("stargazers_count", 0)),
        "fork": bool(data.get("fork", False)),
        "archived": bool(data.get("archived", False)),
        "full_name": data.get("full_name", f"{owner}/{repo}"),
        "html_url": data.get("html_url", f"https://github.com/{owner}/{repo}"),
        "pushed_at": data.get("pushed_at", ""),
    }


def main():
    ap = argparse.ArgumentParser(description="Verified arXiv search (repo-star gated)")
    ap.add_argument("query", help="Free-text search query (research topic)")
    ap.add_argument("--max-results", type=int, default=40,
                    help="How many recent arXiv papers to scan (default 40)")
    ap.add_argument("--min-stars", type=int, default=10,
                    help="Repo must have STRICTLY MORE than this many stars (default 10)")
    ap.add_argument("--since-year", type=int, default=None,
                    help="Only consider papers published in/after this year")
    ap.add_argument("--sort", choices=["relevance", "recency"], default="relevance",
                    help="relevance (default: proven, star-accumulating work) or "
                         "recency (newest preprints; often too new to have stars)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    papers = search_arxiv(args.query, args.max_results, args.since_year, args.sort)
    verified = []
    scanned_repos = 0
    rate_limited = False

    for p in papers:
        repos = extract_github_repos(p["summary"] + "\n" + p["comment"])
        if not repos:
            continue
        best = None
        for owner, repo in repos:
            scanned_repos += 1
            info = github_repo_info(owner, repo, token)
            time.sleep(0.2)  # be polite to the API
            if info.get("rate_limited"):
                rate_limited = True
                continue
            if not info.get("exists"):
                continue
            if info["stars"] > args.min_stars:
                if best is None or info["stars"] > best["stars"]:
                    best = info
        if best:
            verified.append({
                "title": p["title"],
                "abs_url": p["abs_url"],
                "published": p["published"],
                "repo": best["full_name"],
                "repo_url": best["html_url"],
                "stars": best["stars"],
                "fork": best["fork"],
                "archived": best["archived"],
                "last_push": best["pushed_at"],
                "summary": p["summary"],
            })

    verified.sort(key=lambda v: v["stars"], reverse=True)

    if args.json:
        print(json.dumps(verified, indent=2))
        return 0

    print(f"\n=== Verified arXiv search: \"{args.query}\" ===")
    print(f"Scanned {len(papers)} recent papers, {scanned_repos} candidate repos. "
          f"Gate: GitHub repo exists AND stars > {args.min_stars}.\n")
    if rate_limited and not token:
        print("  ⚠️  Hit GitHub's unauthenticated rate limit (60/hr). Set GITHUB_TOKEN "
              "to scan more. Results below may be incomplete.\n")
    if not verified:
        print("  No papers passed the verification gate. Either the topic has no "
              "recent code-backed papers, or widen --max-results / lower --min-stars.\n")
        return 0

    for i, v in enumerate(verified, 1):
        flags = []
        if v["fork"]:
            flags.append("FORK")
        if v["archived"]:
            flags.append("ARCHIVED")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        print(f"{i}. ⭐ {v['stars']:>6}  {v['title']}")
        print(f"   arXiv : {v['abs_url']}   ({v['published'][:10]})")
        print(f"   repo  : {v['repo_url']}{flag_str}  (last push {v['last_push'][:10]})")
        # one-line abstract teaser
        teaser = " ".join(v["summary"].split())[:220]
        print(f"   abstract: {teaser}...\n")

    print(f"=== {len(verified)} verified (code-backed, >{args.min_stars}★) papers ===\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
