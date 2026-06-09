# Contributing

Thanks for taking the time to contribute. This document covers the workflow
for getting changes reviewed and merged.

## Ground rules

- **Open an issue before opening a PR** for anything beyond a small fix. New
  work that doesn't fit under an existing issue should get one first so the
  design can be discussed before code lands.
- **One logical change per PR.** Multiple unrelated fixes in a single PR make
  review slower and revert riskier. If you're cleaning up while you're in the
  file, ship the cleanup as a separate PR.
- **Tests required for behavior changes.** A bug fix without a regression
  test is incomplete. The test suite is `pytest tests/ -v`; the project also
  has E2E tests gated on `-m e2e` that need the `hermes-nodes` Go binary.
- **Don't reformat unrelated code.** Keep diffs focused. A drive-by `black`
  pass makes the reviewer read more than they need to.

## Local setup

```bash
git clone https://github.com/blaspat/hermes-nodes-plugin.git
cd hermes-nodes-plugin
python3.11 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# Run the test suite
pytest tests/ -v

# Run the linter (ruff is the canonical choice; CI uses the same config)
ruff check src/ tests/
```

## Branch naming

- `fix/issue-<N>-<short-slug>` for bug fixes that reference an issue.
- `feat/<short-slug>` for new functionality.
- `docs/<short-slug>` for README, comments, or doc-only changes.
- `chore/<short-slug>` for tooling, CI, or housekeeping.

## Commit messages

Conventional Commits style. The subject line is what reviewers scan first;
make it count.

```
<type>(<scope>): <imperative summary> (#<issue>)

<body — what changed and why, in present tense>
```

Types: `fix`, `feat`, `docs`, `chore`, `refactor`, `test`, `perf`.

## Pull request flow

1. **Push the branch** as soon as the first commit lands. Don't wait for
   "polish" — a draft PR is fine, and a visible branch is much easier to
   collaborate on than a local commit.
2. **Open the PR** with the `Closes #<issue>` keyword in the body so GitHub
   auto-closes the issue on merge. PR description should be 2-3 lines:
   what changed, why, and any decisions worth flagging. The commit message
   body has the detail.
3. **CI must be green** before review. The repo runs the test suite and
   ruff on every push; fix what it flags.
4. **Address review comments** by pushing follow-up commits on the same
   branch — don't force-push mid-review unless the reviewer asks for a
   rebase. Squash-merge is the default; the PR title becomes the commit
   subject.

## Security issues

Don't file public issues for security bugs. See
[`SECURITY.md`](./SECURITY.md) for the disclosure policy.

## Code style

- Python 3.10+ (the `pyproject.toml` `requires-python` is the source of
  truth). Type hints throughout.
- Public functions and modules get docstrings. The codebase uses the
  Google-ish section style (`"""...""",` then a blank line and a
  `Parameters:` / `Notes:` / `Why?` section). Match the style of the
  file you're editing.
- Logging over `print` for anything except the CLI's user-facing output
  (which goes to stdout per the `cli.py` contract).
- The plugin's `register()` callback is **defensive** — never let a plugin
  load failure break the host. New entry points should follow the same
  swallow-and-log pattern.

## Tests

- Unit tests live in `tests/` and mirror the source layout.
- E2E tests in `tests/e2e/` are gated behind `@pytest.mark.e2e` and need
  the Go binary from [`hermes-nodes`](https://github.com/blaspat/hermes-nodes)
  built and on `PATH`. Run them with `pytest tests/e2e/ -v -m e2e`.
- New features need at least one happy-path test and one failure-path test
  in the same PR.

## License

By contributing, you agree that your contributions will be licensed under
the MIT License — see [`LICENSE`](./LICENSE).
