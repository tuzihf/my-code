"""apply_patch(unified diff)测试。"""
from minicore.patch import apply_unified_diff, _parse_hunks


def test_single_hunk():
    new, err = apply_unified_diff("a\nb\nc\nd\n", "@@ -2,2 +2,2 @@\n b\n-c\n+X\n")
    assert err == ""
    assert new == "a\nb\nX\nd\n"


def test_multi_hunk():
    new, err = apply_unified_diff(
        "a\nb\nc\nd\ne\n",
        "@@ -1,1 +1,1 @@\n-a\n+A\n@@ -5,1 +5,1 @@\n-e\n+E\n",
    )
    assert err == ""
    assert new == "A\nb\nc\nd\nE\n"


def test_fuzz_line_number_offset():
    """hunk 头行号不准时,靠上下文全局搜索仍能匹配。"""
    new, err = apply_unified_diff("x\na\nb\nc\ny\n", "@@ -10,2 +10,2 @@\n a\n-b\n+B\n")
    assert err == ""
    assert new == "x\na\nB\nc\ny\n"


def test_no_match_reports_error():
    new, err = apply_unified_diff("a\nb\n", "@@ -1,1 +1,1 @@\n-zzz\n+Z\n")
    assert err != ""
    assert new == "a\nb\n"  # 失败返回原内容


def test_parse_hunks_ignores_headers():
    patch = "--- a/f.py\n+++ b/f.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
    hunks = _parse_hunks(patch)
    assert len(hunks) == 1
    assert hunks[0]["old_start"] == 1
