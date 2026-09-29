# Releasing PromptQuery

This document is for project maintainers. Most users should ignore it.

## One-time setup (per PyPI account)

1. **Create the PyPI account** at https://pypi.org/account/register/ if you don't have one. Enable 2FA.
2. **Reserve the project name** by uploading once manually — see below.
3. **Configure trusted publishing (recommended, no tokens to store)**:
   - Go to https://pypi.org/manage/account/publishing/ → *Add a new pending publisher*.
   - Project: `promptquery`. Owner: `Cyberfilo`. Repository: `promptquery`. Workflow: `publish.yml`. Environment: `pypi`.
   - After this is configured, future releases are published automatically by GitHub Actions on `v*` tag pushes (no API token needed in the repo).

## Cutting a release (post-setup)

```bash
# 1. Bump the version in two places:
#    - src/promptquery/__init__.py  →  __version__
#    - pyproject.toml               →  [project] version
#    Keep them in sync.

# 2. Update the CHANGELOG (if/when we add one) and README badge.

# 3. Run the full test suite + a benchmark sanity check:
.venv/bin/pytest -q
.venv/bin/python -m eval.retrieval --quiet --fail-under 0.9

# 4. Build + validate locally (optional but reassuring):
rm -rf dist/
.venv/bin/python -m build
.venv/bin/twine check dist/*

# 5. Commit, tag, push:
git add -A
git commit -m "vX.Y.Z: <one-line summary>"
git tag -a vX.Y.Z -m "PromptQuery vX.Y.Z"
git push origin main
git push origin vX.Y.Z

# 6. Create a GitHub Release pointing at the tag (triggers publishing once
#    trusted publishing is configured; until then, do step 7 manually):
gh release create vX.Y.Z --notes-file path/to/notes.md

# 7. MANUAL publish (until trusted publishing is configured):
.venv/bin/twine upload dist/*
# Provide your PyPI API token when prompted, OR put it in ~/.pypirc:
#   [pypi]
#   username = __token__
#   password = pypi-AgENd...
```

## Verifying a release worked

```bash
# Wait ~30 seconds for PyPI to index, then in a fresh venv:
python3 -m venv /tmp/pq-verify && /tmp/pq-verify/bin/pip install promptquery
/tmp/pq-verify/bin/prq --version    # should print 0.2.0 (or whichever version)
```

## Yanking a bad release

If a release ships with a critical bug:

```bash
# Yank (hide from new installs but keep available for pinned users):
twine upload --skip-existing dist/* --repository pypi   # (no-op if already up)
# Then on PyPI's web UI: Manage releases → vX.Y.Z → Yank release.
```

Don't `--delete` releases from PyPI unless they leak secrets — yanking is the standard.
