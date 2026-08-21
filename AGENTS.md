# AGENTS.md

## Cursor Cloud specific instructions

Signal Desk is a single Python 3.12 project: a multi-horizon options scanner exposed as
a Rich CLI (`python -m odte_scanner ...`) and a Flask web dashboard
(`python -m odte_scanner ui`). There is no separate frontend build — the UI HTML is
rendered server-side from `odte_scanner/ui.py`. See `README.md` for the full command list.

### Environment
- A virtualenv lives at `.venv` (gitignored). Activate with `source .venv/bin/activate`
  before running anything. The startup update script recreates/refreshes it.
- Dependencies come from `requirements.txt` plus an editable install of the package
  (`pip install -e .`). `requirements.txt` is the superset (it adds `pytest`, `lxml`,
  `requests` on top of `pyproject.toml`).

### Test / lint / run
- Tests: `pytest -q` (~200 tests, fully offline, runs in a few seconds).
- There is no configured linter/formatter in this repo; don't invent one.
- Web UI: `python -m odte_scanner ui --host 0.0.0.0 --port 8787`, then open
  `http://127.0.0.1:8787`. The "Scan" buttons hit `/api/scan?mode=...`.

### Non-obvious gotchas
- Scans hit **live Yahoo Finance** over the network. A full focus/liquid scan
  (`python -m odte_scanner scan`) is very slow because it builds walk-forward win-rate
  tables over the whole universe (many 2y history fetches × 4 horizons) — expect 10+
  minutes, and it only writes `outputs/latest_scan.json` at the very end, so a timeout
  leaves nothing. Use `python -m odte_scanner scan --horizon ml6 --no-paper` for a fast
  (~3s) end-to-end sanity check.
- The first `GET /api/snapshot` (and the first dashboard load) triggers the same heavy
  compute synchronously, so the initial request can take many minutes. Results are cached
  to `outputs/ui_snapshot_cache.json` and `outputs/win_rates.json`; subsequent loads are
  ~15s (they still fetch a few live quotes).
- `outputs/` is gitignored and holds all caches, paper journals, and ledgers. Deleting it
  forces the slow cold-start recompute again.
- Tests are hermetic (they don't call Yahoo); only the `scan`/`watch`/`ui` runtime paths
  need network.
