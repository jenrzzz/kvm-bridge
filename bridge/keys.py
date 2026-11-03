"""Bearer keys for the bridge's clients (hob, a person's CLI). The bridge
keeps only their SHA-256 digests, in keys.json, mode 600. The device's own
password is a different thing: it's in the bridge's environment, and no
client ever holds it."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path

PREFIX = "kvb_"
NAME = re.compile(r"\A[a-z0-9][a-z0-9_.-]{0,62}\Z")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Keys:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)

    def load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text() or "{}")

    def save(self, keys: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(keys, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def add(self, name: str) -> str:
        """A new key for `name` (replacing any it had); the token is shown once and never stored."""
        if not NAME.match(name):
            raise ValueError("a key's name is lowercase letters, digits, and . _ -")
        token = PREFIX + secrets.token_urlsafe(32)
        keys = self.load()
        keys[name] = {"digest": digest(token)}
        self.save(keys)
        return token

    def remove(self, name: str) -> bool:
        keys = self.load()
        found = keys.pop(name, None) is not None
        self.save(keys)
        return found

    def names(self) -> list[str]:
        return sorted(self.load())

    def lookup(self, token: str | None) -> str | None:
        """The name of the key `token` is, or None."""
        if not token:
            return None
        wanted = digest(token)
        for name, entry in self.load().items():
            if hmac.compare_digest(entry.get("digest", ""), wanted):
                return name
        return None
