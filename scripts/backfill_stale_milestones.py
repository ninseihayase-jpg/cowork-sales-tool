"""次回MSライフサイクル是正（docs/milestone-lifecycle/DESIGN.md）の既存データ補正・一回限りのスクリプト。

修正（sfa_db.add_activity内の自動完了）は今後の新規書き込みにのみ効くため、本番DBに既に
「本当は終わっているのに未完了のまま残っているMS」（＝そのMSの日付以降に、同じ商談の
活動履歴が既に存在するのに done=0 のまま）が残っている可能性がある。これを検知・補正する。

判定ロジック: 各商談について「活動履歴のoccurred_on最大値」を求め、その日付以前の
未完了MSを完了(done=1)にする。sfa_db.add_activityに組み込んだ自動完了ロジックと同一で、
「これが最初から効いていたらどうなっていたか」を過去分に対して再現するだけなので安全。

使い方（まずdry-runで確認してから適用すること）:
    python3 scripts/backfill_stale_milestones.py            # dry-run（何も書き込まない・既定）
    python3 scripts/backfill_stale_milestones.py --apply     # 実際に書き込む（実行前に自動バックアップ）

環境変数:
    COWORK_SFA_DB - DBファイルパス（省略時 cowork_sfa.db）
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cowork import sfa_db

DB_PATH = os.environ.get("COWORK_SFA_DB", sfa_db.DEFAULT_DB_PATH)


def find_stale_milestones(con) -> list[dict]:
    """未完了なのに、同じ商談の活動履歴が既にそのMS日付以降に存在するMSを検出する（読み取り専用）。"""
    rows = con.execute("""
        SELECT dm.id AS ms_id, dm.deal_id, dm.ms_date, dm.ms_label, dm.ms_type,
               d.deal_name, a.name AS account_name,
               (SELECT MAX(occurred_on) FROM activities WHERE deal_id = dm.deal_id) AS max_activity_date
        FROM deal_milestones dm
        JOIN deals d ON d.id = dm.deal_id
        LEFT JOIN accounts a ON a.id = d.account_id
        WHERE dm.done = 0 AND dm.ms_date IS NOT NULL AND dm.ms_date != ''
        ORDER BY dm.deal_id, dm.ms_date
    """).fetchall()
    stale = []
    for r in rows:
        r = dict(r)
        max_act = r.get("max_activity_date")
        if max_act and r["ms_date"] <= max_act:
            stale.append(r)
    return stale


def report(stale: list[dict]) -> None:
    if not stale:
        print("該当なし。過去分の滞留MSはありません。")
        return
    print(f"検出: {len(stale)}件（未完了だが、同商談に後続の活動履歴が既にあるMS）\n")
    for r in stale:
        acct = r.get("account_name") or "—"
        print(f"  deal_id={r['deal_id']} [{acct} / {r.get('deal_name') or '—'}] "
              f"MS: {r['ms_date']} {r.get('ms_label') or ''}（種別:{r.get('ms_type') or '—'}）"
              f" → 活動履歴の最終日 {r['max_activity_date']} 以前のため完了扱いにします")


def apply_fixes(con, stale: list[dict]) -> int:
    """検出済みの滞留MSを、商談ごとにcomplete_past_milestonesで完了にする。"""
    deal_targets: dict[int, str] = {}
    for r in stale:
        deal_targets[r["deal_id"]] = r["max_activity_date"]
    total = 0
    for deal_id, as_of in deal_targets.items():
        n = sfa_db.complete_past_milestones(con, deal_id, as_of, commit=False)
        total += n
    con.commit()
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="実際に書き込む（既定はdry-run）")
    args = parser.parse_args()

    if not Path(DB_PATH).exists():
        sys.exit(f"DBファイルが見つかりません: {DB_PATH}")

    con = sfa_db.connect(DB_PATH)
    try:
        stale = find_stale_milestones(con)
        report(stale)
        if not stale:
            return
        if not args.apply:
            print(f"\n[dry-run] {len(stale)}件が対象です。実際に反映するには --apply を付けて再実行してください。")
            return

        print("\n適用前にバックアップを作成します...")
        backup_path = sfa_db.backup_now(DB_PATH, tag="pre_milestone_backfill")
        print(f"バックアップ: {backup_path}")

        n = apply_fixes(con, stale)
        print(f"\n完了: {n}件のMSを完了(done=1)にしました。")
    finally:
        con.close()


if __name__ == "__main__":
    main()
