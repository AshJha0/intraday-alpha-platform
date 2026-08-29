"""SplitMix64 pinned-RNG tests (conventions section 3)."""

import json

import pytest

from iap.core.rng import SplitMix64

# Known-answer values for seed 42, computed independently below AND pinned in
# tests/golden/splitmix64.json — every language must reproduce them.


def _reference_splitmix64(seed, n):
    """Independent inline implementation straight from the conventions text."""
    mask = (1 << 64) - 1
    state = seed & mask
    outs = []
    for _ in range(n):
        state = (state + 0x9E3779B97F4A7C15) & mask
        z = state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & mask
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & mask
        outs.append((z ^ (z >> 31)) & mask)
    return outs


def test_known_answer_seed42_first5():
    rng = SplitMix64(42)
    assert [rng.next_u64() for _ in range(5)] == _reference_splitmix64(42, 5)


def test_known_answer_matches_golden_file(golden_dir):
    with open(golden_dir / "splitmix64.json") as f:
        golden = json.load(f)
    assert golden["seed"] == 42
    rng = SplitMix64(golden["seed"])
    assert [rng.next_u64() for _ in range(5)] == golden["first_5_u64"]
    rng = SplitMix64(golden["seed"])
    assert [rng.uniform() for _ in range(5)] == golden["first_5_uniform"]


def test_uniform_definition_exact():
    raw = _reference_splitmix64(42, 1)[0]
    assert SplitMix64(42).uniform() == (raw >> 11) * 2.0**-53


def test_uniform_in_unit_interval():
    rng = SplitMix64(7)
    for _ in range(10_000):
        u = rng.uniform()
        assert 0.0 <= u < 1.0


def test_same_seed_same_sequence():
    a, b = SplitMix64(999), SplitMix64(999)
    assert [a.next_u64() for _ in range(100)] == [b.next_u64() for _ in range(100)]


def test_different_seeds_differ():
    a, b = SplitMix64(1), SplitMix64(2)
    assert [a.next_u64() for _ in range(10)] != [b.next_u64() for _ in range(10)]


def test_state_wraps_to_64_bits():
    rng = SplitMix64((1 << 64) - 1)  # max state: first add wraps
    v = rng.next_u64()
    assert 0 <= v < (1 << 64)
    assert 0 <= rng.state < (1 << 64)


def test_below_and_randint_bounds():
    rng = SplitMix64(5)
    for _ in range(1000):
        assert 0 <= rng.below(7) < 7
        assert 3 <= rng.randint(3, 9) <= 9


def test_below_rejects_nonpositive():
    with pytest.raises(ValueError):
        SplitMix64(1).below(0)


def test_exponential_positive_and_deterministic():
    a, b = SplitMix64(11), SplitMix64(11)
    xs = [a.exponential(2.0) for _ in range(100)]
    assert xs == [b.exponential(2.0) for _ in range(100)]
    assert all(x >= 0.0 for x in xs)
    with pytest.raises(ValueError):
        SplitMix64(1).exponential(0.0)


def test_split_derives_independent_stream():
    rng = SplitMix64(42)
    child = rng.split()
    assert isinstance(child, SplitMix64)
    assert child.next_u64() != rng.next_u64()
