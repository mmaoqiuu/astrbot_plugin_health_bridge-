import pytest
from astrbot_plugin_health_bridge.health_logic import (
    InvalidPayloadError,
    extract_date,
    normalize_payload,
)


def test_extract_date_rejects_invalid():
    """非法 date 必须抛，不能再兜底成今天。"""
    with pytest.raises(InvalidPayloadError):
        extract_date({"date": "2026/09/11"})
    with pytest.raises(InvalidPayloadError):
        extract_date({"date": "2026-13-40"})
    with pytest.raises(InvalidPayloadError):
        extract_date({})  # 缺 date


def test_normalize_payload_rejects_invalid_date():
    with pytest.raises(InvalidPayloadError):
        normalize_payload({"date": "not-a-date", "steps": 100})