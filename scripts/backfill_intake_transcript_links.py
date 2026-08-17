"""既存データの取り込み文字起こし紐づけ(intake_transcript_id)バックフィル・一回限りのスクリプト。

intake_transcript_id列（活動履歴/ヒアリング結果/セッション/リッチメモの出典を追う紐づけ）は
2026-08-17に導入した新しい列のため、それより前に作られた既存データはNULLのまま残っている。

判定ロジック: エンティティ（商談または論点）ごとに「取り込み文字起こしが1件だけ」かつ
「そのエンティティの未紐づけの対象行（rich_notes/activities/hearing_results/hearing_sessions）が
1件だけ」の場合のみ、確実な1:1関係とみなして自動で紐づける。文字起こしが2件以上ある、
または未紐づけの対象行が2件以上あるエンティティは「どちらがどれの出典か」を後から復元できないため
自動判定せず、確認対象として一覧表示するだけに留める（誤った紐づけを作らないための安全策）。

使い方（まずdry-runで確認してから適用すること）:
    python3 scripts/backfill_intake_transcript_links.py            # dry-run（何も書き込まない・既定）
    python3 scripts/backfill_intake_transcript_links.py --apply     # 実際に書き込む（実行前に自動バックアップ）

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


def report(result: dict) -> None:
    apply_list = result["apply"]
    ambiguous = result["ambiguous"]
    if not apply_list and not ambiguous:
        print("該当なし。バックフィル対象はありません。")
        return
    if apply_list:
        print(f"確実に紐づけ可能: {len(apply_list)}件\n")
        for item in apply_list:
            print(f"  {item['table']}#{item['row_id']} → intake_transcripts#{item['intake_transcript_id']}"
                  f"（{item['kind']}#{item['entity_id']}）")
    if ambiguous:
        print(f"\n曖昧なため自動判定をスキップ（要手動確認）: {len(ambiguous)}件\n")
        for a in ambiguous:
            print(f"  {a['kind']}#{a['entity_id']}: {a['reason']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="実際に書き込む（既定はdry-run）")
    args = parser.parse_args()

    if not Path(DB_PATH).exists():
        sys.exit(f"DBファイルが見つかりません: {DB_PATH}")

    con = sfa_db.connect(DB_PATH)
    try:
        result = sfa_db.find_intake_transcript_backfill_candidates(con)
        report(result)
        if not result["apply"]:
            return
        if not args.apply:
            print(f"\n[dry-run] {len(result['apply'])}件が対象です。実際に反映するには --apply を付けて再実行してください。")
            return

        print("\n適用前にバックアップを作成します...")
        backup_path = sfa_db.backup_now(DB_PATH, tag="pre_intake_link_backfill")
        print(f"バックアップ: {backup_path}")

        n = sfa_db.apply_intake_transcript_backfill(con, result["apply"])
        print(f"\n完了：{n}件を紐づけました。")
    finally:
        con.close()


if __name__ == "__main__":
    main()
