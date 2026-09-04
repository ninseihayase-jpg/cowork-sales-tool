"""開発要件一覧（#165, docs未整備・chatでのユーザー確定仕様のみ）の初回バックフィル・一回限りのスクリプト。

対象: 商談ステージが「提案」以降（DELIVERY_TRIGGER_STAGES = 提案/クロージング/受注）でopenの商談、
および全Delivery案件。それぞれ dev_requirements に行がまだ無ければ1件作成する
（sfa_db.ensure_dev_requirement_for_deal / ensure_dev_requirement_for_delivery と同じ判定・冪等）。

新規作成した行のみ、「プロジェクト概要・ゴール」をClaude Haikuで自動生成する
（入力=商談の現状メモ＋直近の活動履歴。ユーザー確定・2026-09-04）。生成に失敗しても
行自体は作成済みのまま残る（overviewは空欄）。

使い方（まずdry-runで確認してから適用すること）:
    python3 scripts/backfill_dev_requirements.py            # dry-run（何も書き込まない・既定）
    python3 scripts/backfill_dev_requirements.py --apply     # 実際に書き込む（実行前に自動バックアップ）

環境変数:
    COWORK_SFA_DB - DBファイルパス（省略時 cowork_sfa.db）
    ANTHROPIC_API_KEY - 未設定ならHaiku生成はスキップされ、overviewは空欄のまま作成される。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cowork import sfa_db
from cowork import webapp

DB_PATH = __import__("os").environ.get("COWORK_SFA_DB", sfa_db.DEFAULT_DB_PATH)


def find_candidates(con) -> list[dict]:
    """まだdev_requirements行が無い、対象の商談/Deliveryを検出する（読み取り専用）。"""
    out = []
    deals = [d for d in sfa_db.list_deals(con, status="open")
             if (d.get("stage") or "") in sfa_db.DELIVERY_TRIGGER_STAGES]
    for d in deals:
        if not sfa_db.get_dev_requirement_by_link(con, "deal", d["id"]):
            out.append({"link_type": "deal", "link_id": d["id"],
                       "account_name": d.get("account_name"), "project_name": d.get("deal_name"),
                       "deal_id": d["id"]})
    for dv in sfa_db.list_deliveries(con):
        if not sfa_db.get_dev_requirement_by_link(con, "delivery", dv["id"]):
            out.append({"link_type": "delivery", "link_id": dv["id"],
                       "account_name": dv.get("account_name"),
                       "project_name": dv.get("title") or dv.get("deal_name"),
                       "deal_id": dv.get("deal_id")})
    return out


def report(candidates: list[dict]) -> None:
    if not candidates:
        print("該当なし。開発要件一覧に追加が必要な商談/Deliveryはありません。")
        return
    print(f"検出: {len(candidates)}件（開発要件一覧に未作成の商談/Delivery）\n")
    for c in candidates:
        kind = "商談" if c["link_type"] == "deal" else "Delivery"
        print(f"  [{kind}] {c.get('account_name') or '—'} / {c.get('project_name') or '—'}"
              f"（{c['link_type']}_id={c['link_id']}）")


def _build_overview_context(con, deal_id: int | None) -> str:
    """Haikuへの入力: 商談の現状メモ＋直近5件の活動履歴（ユーザー確定の入力ソース）。
    現状メモ・活動履歴のどちらも無い場合は空文字を返す（呼び出し側でHaiku呼び出し自体を
    スキップする）。プレースホルダー文言(「(空欄)」等)をそのまま渡すと、Haikuが
    「申し訳ございませんが情報が不足しており...」という謝罪文をそのままoverviewに
    書き込んでしまう不具合が実際に発生したため（2026-09-04ユーザー報告・初回バックフィルで
    多数の行に混入）。"""
    if not deal_id:
        return ""
    deal = sfa_db.get_deal(con, deal_id)
    if not deal:
        return ""
    note = (deal.get("note") or "").strip()
    activities = sfa_db.list_activities(con, deal_id)[:5]
    if not note and not activities:
        return ""
    act_lines = "\n".join(
        f"- {a.get('occurred_on') or '(日付不明)'} [{a.get('type') or '—'}] {(a.get('body') or '').strip()[:200]}"
        for a in activities
    ) or "(活動履歴なし)"
    return f"現状メモ:\n{note or '(空欄)'}\n\n直近の活動履歴:\n{act_lines}"


def generate_overview(con, deal_id: int | None) -> str:
    """商談の現状メモ＋直近活動履歴からHaikuで「プロジェクト概要・ゴール」を生成する。
    ANTHROPIC_API_KEY未設定・生成失敗時は空文字を返す（呼び出し側はoverview空欄のまま進める）。"""
    context = _build_overview_context(con, deal_id)
    if not context:
        return ""
    prompt = (
        "次の商談の現状メモと直近の活動履歴から、開発チームに向けた「プロジェクト概要・ゴール」を"
        "2〜3文程度で簡潔にまとめてください。事実として書かれていないことは推測で補わないでください。"
        "JSONや見出しは不要、本文のみを出力してください。\n\n" + context
    )
    return webapp._call_claude_haiku(prompt, timeout=20, max_wait=25, max_tokens=300)


def apply_backfill(con, candidates: list[dict]) -> int:
    """検出済みの候補を作成し、可能な範囲でoverviewをHaiku生成する。作成件数を返す。"""
    n = 0
    for c in candidates:
        rid = sfa_db.upsert_dev_requirement(con, link_type=c["link_type"], link_id=c["link_id"])
        n += 1
        try:
            overview = generate_overview(con, c.get("deal_id"))
        except Exception as _e:  # noqa: BLE001
            print(f"  [警告] overview生成失敗（{c['link_type']}_id={c['link_id']}）: {_e}")
            overview = ""
        if overview:
            sfa_db.set_dev_requirement_field(con, rid, "overview", overview)
    return n


# ── overviewに謝罪文が混入した行の修正（2026-09-04ユーザー報告） ────────────────
# 初回バックフィルで、現状メモ・活動履歴がどちらも無い商談に対し
# 「申し訳ございませんが情報が不足しており...」という謝罪文をそのままoverviewに
# 書き込んでしまっていた（_build_overview_contextの修正で今後は発生しない）。
# 既に作成済みの行はこのモードで再生成する。
_APOLOGY_MARKERS = ("申し訳ございません", "申し訳ありません", "情報が不足")


def _resolve_deal_id(con, row: dict) -> int | None:
    if row["link_type"] == "deal":
        return row["link_id"]
    if row["link_type"] == "delivery":
        dv = sfa_db.get_delivery(con, row["link_id"])
        return dv.get("deal_id") if dv else None
    return None


def find_apology_overview_rows(con) -> list[dict]:
    return [r for r in sfa_db.list_dev_requirements(con)
            if r.get("overview") and any(m in r["overview"] for m in _APOLOGY_MARKERS)]


def fix_overviews(con, rows: list[dict]) -> int:
    """謝罪文が混入したoverviewを、修正済みロジックで再生成する（材料が無ければ空欄に戻す）。"""
    n = 0
    for r in rows:
        deal_id = _resolve_deal_id(con, r)
        try:
            overview = generate_overview(con, deal_id)
        except Exception as _e:  # noqa: BLE001
            print(f"  [警告] overview再生成失敗（id={r['id']}）: {_e}")
            overview = ""
        sfa_db.set_dev_requirement_field(con, r["id"], "overview", overview or None)
        n += 1
    return n


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="実際に書き込む（既定はdry-run）")
    parser.add_argument("--fix-overviews", action="store_true",
                        help="新規作成は行わず、overviewに謝罪文が混入した既存行だけを再生成する")
    args = parser.parse_args()

    if not Path(DB_PATH).exists():
        sys.exit(f"DBファイルが見つかりません: {DB_PATH}")

    con = sfa_db.connect(DB_PATH)
    try:
        if args.fix_overviews:
            rows = find_apology_overview_rows(con)
            if not rows:
                print("該当なし。overviewに謝罪文が混入した行はありません。")
                return
            print(f"検出: {len(rows)}件（overviewに謝罪文が混入している行）\n")
            for r in rows:
                print(f"  id={r['id']} [{r.get('account_name') or '—'} / {r.get('project_name') or '—'}]")
            if not args.apply:
                print(f"\n[dry-run] {len(rows)}件が対象です。実際に反映するには "
                      "--apply --fix-overviews を付けて再実行してください。")
                return
            print("\n適用前にバックアップを作成します...")
            backup_path = sfa_db.backup_now(DB_PATH, tag="pre_dev_requirements_overview_fix")
            print(f"バックアップ: {backup_path}")
            n = fix_overviews(con, rows)
            print(f"\n完了: {n}件のoverviewを再生成しました。")
            return

        candidates = find_candidates(con)
        report(candidates)
        if not candidates:
            return
        if not args.apply:
            print(f"\n[dry-run] {len(candidates)}件が対象です。実際に反映するには --apply を付けて再実行してください。")
            print("（--apply時、各行の「プロジェクト概要・ゴール」をClaude Haikuで自動生成します。"
                  "ANTHROPIC_API_KEY未設定の場合は空欄のまま作成されます）")
            return

        print("\n適用前にバックアップを作成します...")
        backup_path = sfa_db.backup_now(DB_PATH, tag="pre_dev_requirements_backfill")
        print(f"バックアップ: {backup_path}")

        n = apply_backfill(con, candidates)
        print(f"\n完了: {n}件を開発要件一覧に追加しました。")
        print("xlsx出力は本番サーバー起動中に /dev-requirements/export.xlsx から取得できます。")
    finally:
        con.close()


if __name__ == "__main__":
    main()
