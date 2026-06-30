from __future__ import annotations

from harness.policy.guard import is_protected, protected_violations

_PATTERNS = ["tests/spec/**"]


def test_protected_dir_prefix() -> None:
    assert is_protected("tests/spec/test_api.py", _PATTERNS)
    assert is_protected("tests/spec/sub/test_x.py", _PATTERNS)


def test_non_protected_paths() -> None:
    assert not is_protected("tests/unit/test_x.py", _PATTERNS)
    assert not is_protected("src/app.py", _PATTERNS)
    assert not is_protected("tests/spec_helpers.py", _PATTERNS)  # не каталог tests/spec/


def test_leading_dot_slash_normalized() -> None:
    assert is_protected("./tests/spec/test_api.py", _PATTERNS)


def test_violations_sorted_unique() -> None:
    changed = [
        "src/app.py",
        "tests/spec/test_b.py",
        "tests/spec/test_a.py",
        "tests/spec/test_a.py",
    ]
    assert protected_violations(changed, _PATTERNS) == [
        "tests/spec/test_a.py",
        "tests/spec/test_b.py",
    ]


def test_no_patterns_means_no_protection() -> None:
    assert protected_violations(["tests/spec/test_a.py"], []) == []


def test_explicit_glob_pattern() -> None:
    assert is_protected("acceptance/x.yaml", ["acceptance/*.yaml"])
    assert not is_protected("acceptance/x.txt", ["acceptance/*.yaml"])
