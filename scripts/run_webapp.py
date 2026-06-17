"""営業支援ツール（フェーズ2-1）ブラウザ入力画面を起動する。

  python scripts/run_webapp.py            # http://localhost:8787
  PORT=9000 python scripts/run_webapp.py  # ポート変更

THEME_API_TOKEN が .env/環境変数にあれば「テーマDBへ同期」ボタンが有効になる。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cowork import sfa_db, webapp  # noqa: E402
from cowork.theme_db import ThemeDBClient  # noqa: E402


def _load_dotenv():
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main():
    _load_dotenv()
    port = int(os.environ.get("PORT", "8787"))
    db_path = os.environ.get("COWORK_SFA_DB", sfa_db.DEFAULT_DB_PATH)

    token = os.environ.get("THEME_API_TOKEN", "").strip()
    client = None
    if token and not token.startswith("（") and not token.startswith("<"):
        client = ThemeDBClient(os.environ.get("THEME_API_URL", "https://hisho-ohxe.onrender.com"), token)
        print("テーマDB同期: 有効")
    else:
        print("テーマDB同期: 無効（THEME_API_TOKEN未設定）")

    webapp.start(db_path=db_path, port=port, theme_client=client)


if __name__ == "__main__":
    main()
