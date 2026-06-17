"""テーマDB（秘書/Hisho の Render 上 SQLite）への薄いクライアント。

debug_api の /api/execute（INSERT/UPDATE/DELETE/SELECT のみ・token認証）を叩く。
独立プロジェクトから疎結合でテーマDBを更新するための窓口。
"""

from __future__ import annotations

import json
import urllib.request


class ThemeDBClient:
    def __init__(self, api_url: str, token: str, timeout: int = 30):
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def execute(self, sql: str, params: list) -> dict:
        body = json.dumps({"sql": sql, "params": params}).encode()
        req = urllib.request.Request(
            f"{self.api_url}/api/execute?token={self.token}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())

    def existing_theme_ids(self) -> set[int]:
        result = self.execute("SELECT id FROM todos WHERE node_type='theme'", [])
        return {r["id"] for r in result.get("rows", [])}
