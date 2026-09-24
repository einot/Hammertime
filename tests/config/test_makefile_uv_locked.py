"""Every `uv run` recipe in the repository's `Makefile` carries `--locked`.

ADR-0018 (`docs/adr/0018-coder-bash-policy-literal-commands-and-a-tripwire.md`),
decision 5, records the owner's decision of 2026-09-24 that `--locked` is
required everywhere: in the Bash guard, in CLAUDE.md's gates and in the
`Makefile`'s `uv run` recipes (brief C3). Decision 8 explains why the last one
matters to the guard: `make typecheck` is an allowed coder command, the guard
cannot see inside make, so the gate carries `--locked` only because its recipe
does. This module pins that (brief T1, group N). It fails until C3 lands.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"


def uv_run_recipe_lines() -> list[tuple[int, str]]:
    """(line number, line) for every recipe line that runs `uv run`.

    A recipe line is one that begins with a tab. `uv run` is found by words, so
    spacing does not matter.
    """
    assert MAKEFILE.is_file(), f"{MAKEFILE} does not exist"
    found: list[tuple[int, str]] = []
    for number, line in enumerate(MAKEFILE.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.startswith("\t"):
            continue
        tokens = line.split()
        if any(tokens[i : i + 2] == ["uv", "run"] for i in range(len(tokens))):
            found.append((number, line))
    return found


def test_makefile_has_uv_run_recipes() -> None:
    """The check below must not pass vacuously."""
    assert uv_run_recipe_lines(), f"no recipe line in {MAKEFILE} runs `uv run`"


def test_every_uv_run_recipe_carries_locked_immediately_after_uv_run() -> None:
    offenders = []
    for number, line in uv_run_recipe_lines():
        tokens = line.split()
        for i in range(len(tokens) - 1):
            if tokens[i : i + 2] == ["uv", "run"] and tokens[i + 2 : i + 3] != ["--locked"]:
                offenders.append(f"line {number}: {line.strip()}")
    assert not offenders, (
        "every `uv run` in a Makefile recipe must be `uv run --locked` (ADR-0018 decision 5, "
        f"the owner's decision; decision 8; brief C3). Offending lines: {offenders}"
    )
