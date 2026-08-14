"""permissions / model_switcher / api_retry 测试。"""
import pytest

from minicore.permissions import PermissionManager
from minicore.model_switcher import ModelSwitcher
from minicore.api_retry import with_retry, _is_retryable


class TestPermissions:
    def test_readonly_always_allowed(self):
        pm = PermissionManager(cwd=".", prompt=lambda req: {"decision": "deny_once"})
        allowed, _ = pm.check_permission("read_file", {"path": "a.py"})
        assert allowed is True  # 只读不问用户

    def test_sensitive_denied(self):
        pm = PermissionManager(cwd=".", prompt=lambda req: {"decision": "deny_once"})
        allowed, reason = pm.check_permission("write_file", {"path": "x.txt"})
        assert allowed is False

    def test_sensitive_allowed(self):
        pm = PermissionManager(cwd=".", prompt=lambda req: {"decision": "allow_once"})
        allowed, _ = pm.check_permission("run_command", {"command": "ls"})
        assert allowed is True

    def test_decisions_recorded(self):
        pm = PermissionManager(cwd=".", prompt=lambda req: {"decision": "allow_once"})
        pm.check_permission("write_file", {"path": "x"})
        assert pm.decisions.get("write_file") == "allow_once"

    def test_allow_persistent(self):
        calls = {"n": 0}
        def prompt(req):
            calls["n"] += 1
            return {"decision": "allow"}
        pm = PermissionManager(cwd=".", prompt=prompt)
        assert pm.check_permission("write_file", {"path": "a"})[0] is True
        assert pm.check_permission("write_file", {"path": "b"})[0] is True
        assert calls["n"] == 1  # 第二次复用历史决定,不再询问

    def test_deny_persistent(self):
        calls = {"n": 0}
        def prompt(req):
            calls["n"] += 1
            return {"decision": "deny"}
        pm = PermissionManager(cwd=".", prompt=prompt)
        assert pm.check_permission("write_file", {})[0] is False
        assert pm.check_permission("write_file", {})[0] is False
        assert calls["n"] == 1  # 第二次直接拒绝,不再询问


class TestModelSwitcher:
    def _make(self):
        class Fake:
            def __init__(self, n): self.name = n
        return ModelSwitcher(
            models=[Fake("a"), Fake("b"), Fake("c")],
            model_names=["a", "b", "c"],
        )

    def test_current_is_first(self):
        sw = self._make()
        assert sw.current_name == "a"

    def test_switch_to_next(self):
        sw = self._make()
        r = sw.switch_to_next("超时")
        assert r.success is True
        assert sw.current_name == "b"

    def test_switch_exhausted(self):
        sw = self._make()
        sw.switch_to_next("1"); sw.switch_to_next("2")
        r = sw.switch_to_next("3")
        assert r.success is False
        assert "没有更多" in r.reason

    def test_switch_count(self):
        sw = self._make()
        sw.switch_to_next("1")
        sw.switch_to_next("2")
        assert sw.get_switch_count() == 2


class TestApiRetry:
    def test_is_retryable(self):
        assert _is_retryable(Exception("service temporarily unavailable"))
        assert _is_retryable(Exception("request timed out"))
        assert _is_retryable(Exception("500 internal error"))

    def test_is_not_retryable(self):
        assert not _is_retryable(Exception("invalid api key 401"))
        assert not _is_retryable(Exception("bad request 400"))

    def test_retries_then_succeeds(self):
        calls = [0]
        def flaky():
            calls[0] += 1
            if calls[0] < 3:
                raise Exception("temporarily overloaded")
            return "ok"
        assert with_retry(flaky, max_retries=3, base_delay=0.01) == "ok"
        assert calls[0] == 3

    def test_non_retryable_raises_immediately(self):
        calls = [0]
        def bad():
            calls[0] += 1
            raise Exception("invalid api key 401")
        with pytest.raises(Exception):
            with_retry(bad, max_retries=3, base_delay=0.01)
        assert calls[0] == 1  # 只调了一次,没重试

    def test_retry_exhausted_raises(self):
        calls = [0]
        def always_fail():
            calls[0] += 1
            raise Exception("temporarily unavailable")
        with pytest.raises(Exception):
            with_retry(always_fail, max_retries=2, base_delay=0.01)
        assert calls[0] == 3  # 首次 + 2 次重试
