import json

from yande_sync.logger import EventLogger


def test_jsonl_is_valid_and_secrets_redacted(tmp_path):
    logger = EventLogger(tmp_path)
    logger.event("failed", detail="Authorization: secret", token="not-visible", note="ok\x1b[2J")
    lines = (tmp_path / "downloads.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["detail"] == "[REDACTED]"
    assert payload["token"] == "[REDACTED]"
    assert "\x1b" not in payload["note"]
    assert "secret" not in (tmp_path / "activity.log").read_text(encoding="utf-8")
