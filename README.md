# Blast Radius entry

Picks the sqlglot tests a patch can break, using maps built from the pinned commit.

- `build.sh` (build time): fetches sqlglot at the pinned commit, runs its suite once with per-test line coverage (`covplugin.py`), and compresses it into `/app/map.marshal` (`build_map.py`).
- `selector.py` (run time): for each patch, skips changes that leave the syntax tree unchanged, maps changed function lines to the tests that ran them, maps changed module/class-level statements to tests that read the names they bind, maps data files to the tests that opened them, then splits the tests over the cheapest number of workers.

`make sample` builds the image and scores it on the 30 sample commits.
