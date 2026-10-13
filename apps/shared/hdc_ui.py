"""Small reusable UI-layout boundary for HDC application collectors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from collector.automation.hdc_backend import HdcBackend


class HdcLayoutUi:
    """Provide observable layout and node traversal without app-specific state logic."""

    layout_operation_name = "application layout dump"

    backend: HdcBackend
    bundle: str

    def layout(self, token: str) -> dict[str, Any]:
        remote = f"/data/local/tmp/{token}.json"
        result = self.backend.run(("shell", "uitest", "dumpLayout", "-p", remote, "-b", self.bundle, "-m", "true"))
        if not result.ok:
            self.backend._raise_for_result(result, operation=self.layout_operation_name)
        local = self.backend.recv(remote, self.backend_target_dir())
        try:
            return json.loads(local.read_text(encoding="utf-8"))
        finally:
            self.backend.run(("shell", "rm", remote))

    @staticmethod
    def backend_target_dir() -> Path:
        return Path("outputs")

    @staticmethod
    def nodes(layout: dict[str, Any]) -> list[dict[str, str]]:
        nodes: list[dict[str, str]] = []
        stack = [layout]
        while stack:
            node = stack.pop()
            attrs = node.get("attributes", {})
            if attrs:
                nodes.append(attrs)
            stack.extend(node.get("children", []))
        return nodes

    def _nodes(self, token: str) -> list[dict[str, str]]:
        return self.nodes(self.layout(token))
