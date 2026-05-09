# Contributing to qa-radar

Thank you for your interest!

## Adding a new RSS source

1. **Verify** the feed URL is publicly accessible (no auth required) by
   `curl -I <feed-url>` and confirm 200 status.
2. **Verify ToS** permits aggregation in the form of *title + ≤100 char
   snippet + link-back*. Sites that explicitly forbid re-distribution
   (e.g. Jiji Press) must NOT be added.
3. Add an entry to `config/sources.yaml` following the existing schema.
4. Open a PR. Include in the PR description:
   - The feed URL you verified
   - The ToS URL and a quoted excerpt covering aggregation
   - Estimated post frequency

## Reporting copyright concerns

If you are a content author or publisher and would like content excluded:

- Open an issue: <https://github.com/Y-Kanekoo/qa-radar/issues>
- Tag with `takedown`
- We aim to respond and act within 7 days

## Development setup

```bash
git clone https://github.com/Y-Kanekoo/qa-radar.git
cd qa-radar
uv sync --all-extras --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Branch policy

- `main` is protected. Direct pushes are not allowed.
- Create `feature/phase-N-<short-name>` branches per phase.
- One PR per phase, with the phase acceptance criteria checklist filled in.
- Squash merge with commit message format `[type] Phase N: <summary>`
  where type ∈ {feat, fix, refactor, docs, test, chore, ops}.

## Coding standards

- Python 3.11+ syntax (no compat with 3.10)
- All comments in Japanese, but identifiers (variable/function/class names) in English
- Type hints required for public functions; no `Any` allowed
- ruff (E, F, I, B, UP, N, SIM, RUF) must pass with line length 100
- pytest coverage target: ≥80%
