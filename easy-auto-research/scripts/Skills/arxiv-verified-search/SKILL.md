---
name: arxiv-verified-search
description: "Search arXiv for the latest solutions on a topic, but return ONLY papers whose abstract advertises a real GitHub repo with more than 10 stars. Use when the Planner wants recent, code-backed prior art to ground a hypothesis — filters out unverified/overstated preprints. PLANNER-ONLY, read-only (network queries to arXiv + GitHub APIs)."
allowed-tools: Bash, Read
---

# arxiv-verified-search — Latest solutions, code-verified (Planner-only)

arXiv is noisy: preprints can carry unverified or overstated claims. This skill searches
arXiv for recent papers on a topic and keeps **only** those that ship a GitHub repo which
actually exists and has **more than 10 stars** — a strong signal the method is real and
reproducible enough that others adopted it. It only queries public APIs and prints a
report; it never clones, writes, or installs.

## When to use
- Before proposing a new hypothesis, to check what recent, *code-backed* approaches exist.
- When you want to differentiate your plan from — or build on — a verified prior method,
  rather than a claim from an unreproduced preprint.

## Verification gate (a paper is returned ONLY if all hold)
1. Its arXiv abstract or comment contains a `github.com/<owner>/<repo>` link.
2. That repo exists on GitHub (API 200).
3. The repo has **strictly more than `--min-stars`** stars (default **10**).

## How to run

The script lives next to this SKILL.md. Run these commands from the release repository root:

```bash
python3 easy-auto-research/scripts/Skills/arxiv-verified-search/scripts/arxiv_verified_search.py \
    "your research topic keywords" [--max-results 40] [--min-stars 10] [--since-year 2024]
```

Examples:
```bash
# recent, code-backed IRM / domain-generalization work
python3 easy-auto-research/scripts/Skills/arxiv-verified-search/scripts/arxiv_verified_search.py \
    "invariant risk minimization domain generalization" --since-year 2024

# stricter bar and machine-readable output
python3 easy-auto-research/scripts/Skills/arxiv-verified-search/scripts/arxiv_verified_search.py \
    "spurious correlation robustness" --min-stars 50 --json
```

**Rate limits:** unauthenticated GitHub allows 60 requests/hour. If you scan many results,
export `GITHUB_TOKEN` (a read-only PAT) first to raise it to 5000/hr — the script uses it
automatically. Without a token it still works; it warns if the limit is hit.

## What it reports (per verified paper)
- ⭐ star count, title, arXiv abstract URL + date
- the verified repo URL (flags FORK / ARCHIVED, last-push date)
- a one-line abstract teaser

Sorted by stars, descending.

## How to use the output in your plan
- Cite the verified paper + its repo in your `## Analysis` when a recent method informs
  your hypothesis ("V6 adopts the reweighting idea from <paper> — repo has 340★, so the
  technique is reproduced, not just claimed").
- Prefer techniques backed by an active (recent last-push), non-fork repo.
- The star gate is a *reproducibility* proxy, not a *quality* proxy — a high-star repo can
  still be wrong for our setting. Treat it as a filter, then judge fit yourself.

## Hard rules
- **Read-only.** Never clone the repo, never install its deps, never copy its code into a
  version dir without an explicit plan step + respecting goal.md's don'ts (e.g. "do not
  install new packages", no edits to No-Touch files).
- The gate proves a repo *exists and is starred*, not that its claims transfer to our task.
- Network-dependent: if arXiv/GitHub are unreachable, the skill reports the failure — fall
  back to reasoning from goal.md + prior versions and note the gap.
