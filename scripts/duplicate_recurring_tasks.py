"""
繰り返し発生（定期複製）タスクの日次複製スクリプト。

事務タスク等の「繰り返し発生」テンプレ（is_recurring=1）を日次で判定し、複製タイミング
（毎月◯日／毎週◯曜）が来た期間分のカードを新規複製する。複製カードは名称に期間サフィックス
（「◯月分」「M/D週分」）を付け、複製先の列は既存の新規起票ルールに従う。
同一期間で二重に複製しない（テンプレの recur_last_period に期間キーを記録して冪等化）。

実行:
  python scripts/duplicate_recurring_tasks.py               # 通常実行（当日JST基準）
  RECUR_TODAY=2026-08-20 python scripts/duplicate_recurring_tasks.py   # 判定日を固定（手動/テスト用）

動作モード（weekly_slack_notify.py と同じ二段構え）:
  - ローカルDB（SFA_DB_PATH / COWORK_SFA_DB のファイル）が存在すればDBを直接操作する。
  - 存在しなければ本番webサービスの /api/tasks/duplicate_recurring をHTTPで叩く
    （Renderのcronはディスク（/data）がwebサービス専属で見えないため、本番はこちら）。

環境変数:
  SFA_DB_PATH / COWORK_SFA_DB - DBパス（省略時 cowork_sfa.db）
  RECUR_TODAY                 - 判定日をYYYY-MM-DDで固定（手動/テスト用）
  SFA_TOOL_URL                - API実行時のwebサービスURL（省略時 https://sfa-crm.onrender.com）
  SFA_API_TOKEN               - /api/tasks/duplicate_recurring 用トークン
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cowork import sfa_db  # noqa: E402

JST = timezone(timedelta(hours=9))
TOOL_URL = os.environ.get("SFA_TOOL_URL", "https://sfa-crm.onrender.com")
SFA_API_TOKEN = os.environ.get("SFA_API_TOKEN", "")


def _db_path() -> str:
    return (os.environ.get("SFA_DB_PATH") or os.environ.get("COWORK_SFA_DB")
            or sfa_db.DEFAULT_DB_PATH)


def _today() -> date:
    ov = os.environ.get("RECUR_TODAY", "").strip()
    if ov:
        return date.fromisoformat(ov)
    return datetime.now(JST).date()


def run_local(db_path: str, today: date) -> int:
    """ローカルDBを直接操作して複製する。新規複製件数を返す。"""
    sfa_db.init_db(db_path)  # 未マイグレーションのDBでも列を揃える（既存DBは無害）
    con = sfa_db.connect(db_path)
    try:
        ids = sfa_db.duplicate_due_recurring_tasks(con, today=today)
    finally:
        con.close()
    print(f"[INFO] （local）判定日 {today.isoformat()} / 複製 {len(ids)}件"
          + (f"（id: {ids}）" if ids else ""))
    return len(ids)


def run_api(today: date) -> int:
    """本番webサービスの複製APIをHTTPで叩く。新規複製件数を返す。"""
    if not SFA_API_TOKEN:
        print("[ERROR] SFA_API_TOKEN が未設定です（API実行にはトークンが必要）。")
        return -1
    url = (f"{TOOL_URL}/api/tasks/duplicate_recurring"
           f"?token={urllib.parse.quote(SFA_API_TOKEN)}")
    payload = json.dumps({"today": today.isoformat()}).encode("utf-8")
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.URLError as e:
        print(f"[ERROR] API呼び出し失敗: {e}")
        return -1
    if not result.get("ok"):
        print(f"[ERROR] API応答エラー: {result}")
        return -1
    n = int(result.get("created", 0))
    print(f"[INFO] （api）判定日 {today.isoformat()} / 複製 {n}件"
          + (f"（id: {result.get('ids')}）" if result.get("ids") else ""))
    return n


def main() -> int:
    today = _today()
    db_path = _db_path()
    if Path(db_path).exists():
        n = run_local(db_path, today)
    else:
        print(f"[INFO] DBが見つからないためAPI経由で実行します（{db_path} 不在）。")
        n = run_api(today)
    return 0 if n >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
