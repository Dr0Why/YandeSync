from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path


class EventLogger:
    SENSITIVE = ("authorization", "cookie", "subscription", "token", "password", "secret", "bearer")

    def __init__(self, log_dir: Path):
        log_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = log_dir / "downloads.jsonl"
        self.logger = logging.getLogger(f"yande_sync.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        handler = logging.FileHandler(log_dir / "activity.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        self.logger.addHandler(handler)
        self._handler = handler

    @classmethod
    def _sanitize(cls, value):
        text = "".join(
            " " if ord(char) < 32 or 127 <= ord(char) < 160 else char for char in str(value)
        )
        lowered = text.lower()
        if any(word in lowered for word in cls.SENSITIVE):
            return "[REDACTED]"
        return text[:500]

    def event(self, event: str, **fields) -> None:
        safe = {
            key: "[REDACTED]" if any(word in key.lower() for word in self.SENSITIVE)
            else self._sanitize(value)
            for key, value in fields.items()
        }
        self.logger.info("%s %s", event.upper(), " ".join(f"{k}={v}" for k, v in safe.items()))
        record = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            **safe,
        }
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def record_exception(self, message: str) -> None:
        self.logger.exception("INTERNAL_ERROR %s", self._sanitize(message))

    def close(self) -> None:
        self.logger.removeHandler(self._handler)
        self._handler.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
