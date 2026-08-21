# Contributing

Contributions should keep Easy-Auto-Research skill-first, focused, and safe for supervised local deep-learning research.

## Before opening a change

- Use Linux, Bash, and Python 3.10 or newer.
- Keep runtime changes narrowly scoped; do not add generated run state, training outputs, datasets, checkpoints, credentials, or secrets.
- Open an issue first for substantial behavior or interface changes.
- Preserve the natural-language skill interface. Internal runtime commands belong in clearly marked implementation sections, not in user-facing instructions.

## Development workflow

Create a branch from the current default branch, make the smallest complete change, and run the release checks from the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python -c 'import pathlib; [compile(p.read_bytes(), str(p), "exec") for p in pathlib.Path(".").rglob("*.py")]'
bash -n install.sh
python -m json.tool easy-auto-research/templates/research_spec.template.json >/dev/null
python -m json.tool easy-auto-research/reference/research_spec.filled_example.json >/dev/null
PYTHONDONTWRITEBYTECODE=1 python easy-auto-research/scripts/init.py --help >/dev/null
PYTHONDONTWRITEBYTECODE=1 python easy-auto-research/scripts/harness.py --help >/dev/null
PYTHONDONTWRITEBYTECODE=1 python easy-auto-research/scripts/watch_session.py --help >/dev/null
PYTHONDONTWRITEBYTECODE=1 python -c 'import pathlib; assert not any(p.name in {"__pycache__", ".pytest_cache", ".ruff_cache"} or p.suffix == ".pyc" for p in pathlib.Path(".").rglob("*"))'
```

Tests must not launch real training, consume GPUs, access private datasets, or depend on credentials. Use temporary directories and mocked process boundaries where the existing suite does so. Add or update tests for behavior changes, especially prompt contracts, path handling, queue ownership, process identity, installation, and release-tree checks.

## Safety and security

Changes must preserve the existing safety gates:

- permission bypass remains explicit opt-in;
- starting or resuming token- or GPU-consuming work requires explicit user confirmation;
- reset and destructive operations remain preview-first and confirmation-gated;
- path traversal, unsafe names, symlink escapes, and unverified process identities fail closed; and
- checkpoint loading keeps its safe default.

Never include credentials, access tokens, private paths, private data, or unredacted logs in an issue, pull request, fixture, or test output. Redact secrets and identifying infrastructure details while retaining the smallest reproducible diagnostic excerpt.

For a suspected vulnerability, do not publish exploit details or secrets in a public issue. Use GitHub private vulnerability reporting if it is available; otherwise open a minimal, redacted issue asking the maintainer for a private contact method.

## Pull requests

Describe the problem, the focused solution, and the checks you ran. Note any change to user-visible skill behavior, permission handling, filesystem operations, process signaling, or resource consumption.
