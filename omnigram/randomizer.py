"""Account data randomizer: pick display names/bios from operator-supplied lists.

Used by the sidebar's Randomizer to fill spare accounts with plausible identities, and by
the profile editor to try a fresh name. The word lists are what the operator pastes in;
nothing here invents identities for deception — it just spreads the operator's own list.
"""
from __future__ import annotations

from collections.abc import Sequence
from random import Random

# Fallback bios when the operator supplies none; deliberately generic.
BIO_PARTS = (
    "Dream big", "Coffee first", "Explorer", "Minimalist", "Just here to read",
    "Keep it simple", "Always curious", "Music and travels", "Quiet observer", "Book lover",
)


def random_name(names: Sequence[str], *, seed: Random | None = None) -> str:
    """One entry from `names`; ValueError on an empty list."""
    if not names:
        raise ValueError("name list is empty")
    return (seed or Random()).choice(list(names))


def random_bio(parts: Sequence[str] = BIO_PARTS, *, seed: Random | None = None) -> str:
    """A short bio: one or two parts joined, picked at random."""
    rng = seed or Random()
    chosen = rng.sample(list(parts), k=min(2, len(parts)))
    return " · ".join(chosen)


def random_profile(names: Sequence[str], parts: Sequence[str] = BIO_PARTS, *, seed: Random | None = None) -> dict:
    """{'first_name', 'last_name', 'about'} — first/last split on the first space of a picked name."""
    rng = seed or Random()
    full = random_name(names, seed=rng)
    first, _, last = full.partition(" ")
    return {"first_name": first, "last_name": last, "about": random_bio(parts, seed=rng)}


def assign_names(count: int, names: Sequence[str], *, seed: Random | None = None) -> dict[int, str]:
    """{index: name} for `count` accounts.

    Names are shuffled and handed out without repetition; once the list runs out, picks repeat.
    Deterministic for a given seed.
    """
    if count <= 0:
        return {}
    if not names:
        raise ValueError("name list is empty")
    rng = seed or Random()
    pool = list(names)
    rng.shuffle(pool)
    return {i: pool[i] if i < len(pool) else rng.choice(pool) for i in range(count)}
