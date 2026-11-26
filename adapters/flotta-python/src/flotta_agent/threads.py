"""Each conversation's messages, kept on the agent's own volume.

Flotta lists an agent's conversations and reopens one by asking the agent
(`GET /threads`, `GET /threads/{id}`), so the agent keeps them. One JSON file a
thread, under `$FLOTTA_DATA/threads/` (`/data` on a Flotta machine, which
survives sleep and restarts).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

#: A thread id Flotta sends is safe as a file name; anything else is refused
#: rather than turned into a path.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
TITLE_LENGTH = 80


class Threads:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, thread_id: str) -> Path | None:
        if not _SAFE_ID.match(thread_id) or thread_id.startswith("."):
            return None
        return self.root / f"{thread_id}.json"

    def get(self, thread_id: str) -> dict[str, Any] | None:
        path = self._path(thread_id)
        if path is None:
            return None
        try:
            found = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            return None
        return found if isinstance(found, dict) else None

    def messages(self, thread_id: str) -> list[dict[str, str]]:
        found = self.get(thread_id)
        return list(found.get("messages") or []) if found else []

    def append(self, thread_id: str, role: str, text: str) -> None:
        path = self._path(thread_id)
        if path is None:
            raise ValueError(f"not a thread id: {thread_id!r}")
        found = self.get(thread_id) or {"id": thread_id, "title": "", "messages": []}
        if not found["title"] and role == "user":
            found["title"] = " ".join(text.split())[:TITLE_LENGTH]
        found["messages"].append({"role": role, "text": text})
        found["updated_at"] = time.time()
        self.root.mkdir(parents=True, exist_ok=True)
        # Written whole and renamed, so a crash never leaves half a thread.
        fd, temp = tempfile.mkstemp(dir=self.root, prefix=".", suffix=".tmp")
        with os.fdopen(fd, "w") as out:
            json.dump(found, out)
        os.replace(temp, path)

    def list(self) -> list[dict[str, Any]]:
        rows = []
        for path in self.root.glob("*.json") if self.root.exists() else []:
            found = self.get(path.stem)
            if found:
                rows.append(
                    {
                        "id": found["id"],
                        "title": found.get("title") or "",
                        "updated_at": found.get("updated_at") or 0,
                    }
                )
        return sorted(rows, key=lambda row: row["updated_at"], reverse=True)
