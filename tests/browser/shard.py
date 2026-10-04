"""Which browser tests a CI shard runs: a greedy split by measured duration.

durations.json holds seconds per test file (a timed run of that file alone). Each test weighs its file's
seconds divided by the file's test count; the heaviest tests are placed first, each on the lightest shard
so far (ties: the lowest shard). One slow file therefore spreads over every shard instead of setting the
length of one. A file missing from durations.json weighs DEFAULT_TEST_SECONDS per test, so a new file
lands on the lightest shards without anyone editing this. Pure and deterministic: the same collected
tests always give the same split, every test lands on exactly one shard.
Refresh the numbers by timing each file (uv run pytest -m browser <file>) when the split drifts."""
import json
from collections import Counter
from pathlib import Path

DEFAULT_TEST_SECONDS = 5.0
DURATIONS = Path(__file__).with_name("durations.json")


def assign(tests, count, durations=None):
    """tests: [(nodeid, file name)]. Returns count lists of nodeids, one per shard."""
    if durations is None:
        durations = json.loads(DURATIONS.read_text(encoding="utf-8"))
    per_file = Counter(name for _, name in tests)
    weight = {nid: durations[name] / per_file[name] if name in durations else DEFAULT_TEST_SECONDS for nid, name in tests}
    shards, load = [[] for _ in range(count)], [0.0] * count
    for nid in sorted(weight, key=lambda n: (-weight[n], n)):
        lightest = load.index(min(load))
        shards[lightest].append(nid)
        load[lightest] += weight[nid]
    return shards
