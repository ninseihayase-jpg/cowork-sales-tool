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


def _auto_seed(db_path: str):
    """deals テーブルが空かつ seed_render.sql が存在する場合にシードを実行する。"""
    import sqlite3
    seed_path = Path(__file__).resolve().parent / "seed_render.sql"
    if not seed_path.exists():
        print("[Seed] seed_render.sql が見つかりません — スキップ", flush=True)
        return
    sfa_db.init_db(db_path)  # テーブルが存在しない場合に先に作成
    con = sqlite3.connect(db_path)
    try:
        count = con.execute("SELECT count(*) FROM deals").fetchone()[0]
        if count == 0:
            print(f"[Seed] deals が空です。{seed_path.name} を実行します...", flush=True)
            con.executescript(seed_path.read_text(encoding="utf-8"))
            con.commit()
            count = con.execute("SELECT count(*) FROM deals").fetchone()[0]
            print(f"[Seed] 完了: deals={count}件", flush=True)
        else:
            print(f"[Seed] deals={count}件 — シードスキップ", flush=True)
    except Exception as e:
        print(f"[Seed] エラー: {e}", flush=True)
    finally:
        con.close()


def main():
    _load_dotenv()
    port = int(os.environ.get("PORT", "8787"))
    db_path = os.environ.get("COWORK_SFA_DB", sfa_db.DEFAULT_DB_PATH)

    _auto_seed(db_path)

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
