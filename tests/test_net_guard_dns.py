"""DNS answers that differ between networks: NAT64, a sinkhole record next to a real one, and clear errors."""
import pytest

from app.net_guard import BlockedTarget, TargetPolicy, _check_address, _usable

POLICY = TargetPolicy()


def test_nat64_address_is_checked_as_the_ipv4_it_carries():
    _check_address("64:ff9b::3416:1a2b", POLICY, "hooks.slack.com")  # 52.22.26.43, public
    with pytest.raises(BlockedTarget):
        _check_address("64:ff9b::7f00:1", POLICY, "hooks.slack.com")  # 127.0.0.1
    with pytest.raises(BlockedTarget):
        _check_address("64:ff9b::a9fe:a9fe", POLICY, "evil.example")  # 169.254.169.254, metadata


def test_safe_records_are_used_when_one_record_is_blocked():
    assert _usable(["0.0.0.0", "52.22.26.43"], POLICY, "hooks.slack.com") == ["52.22.26.43"]


def test_error_names_the_resolved_address():
    with pytest.raises(BlockedTarget, match=r"hooks\.slack\.com \(resolved to 0\.0\.0\.0\)"):
        _usable(["0.0.0.0"], POLICY, "hooks.slack.com")
