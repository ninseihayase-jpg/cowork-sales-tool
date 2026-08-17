"""slack_bot._extract_field の抽出（特に複数行フリーテキスト）検証。"""
from __future__ import annotations

from cowork import slack_bot as sb


TMPL = """—— 商談更新（変更なしは「-」のまま）——
*ステージ: 要件詰め*　　＊アポ / 提案 / クロージング
*次回MS日: -*
*次回MS種別: -*　　＊アポ / タスク
追記メモ: ■ヒアリング内容追記:
【現状】
・汎用AI導入は開始したばかり
・システム部門が推進中
■提案方向:
現場工程変更をトリガーにした自動通知

✅ 確認後「確定」または「ok」と返信すると保存します。
"""


def test_extract_multiline_memo():
    memo = sb._extract_field(TMPL, "追記メモ")
    assert memo is not None
    # 先頭行＋後続の箇条書き・見出しをすべて含む（先頭行だけに切れない）
    assert "■ヒアリング内容追記:" in memo
    assert "・システム部門が推進中" in memo
    assert "■提案方向:" in memo
    # フッタ(✅)は取り込まない
    assert "確定" not in memo
    assert memo.count("\n") >= 5


def test_extract_singleline_and_bold_and_dash():
    assert sb._extract_field(TMPL, "ステージ") == "要件詰め"   # 太字＋ヒント除去
    assert sb._extract_field(TMPL, "次回MS日") is None          # '-' はNone
    assert sb._extract_field(TMPL, "次回MS種別") is None        # 太字の '-' もNone


def test_memo_stops_at_next_label():
    t = "追記メモ: 一行目\n続き\n担当: 早瀬\n"
    assert sb._extract_field(t, "追記メモ") == "一行目\n続き"   # 次ラベル(担当:)で停止
    assert sb._extract_field(t, "担当") == "早瀬"


def test_extract_field_accepts_fullwidth_colon():
    """全角コロン「：」で入力しても半角「:」と同様に認識する（サイレント無視バグの修正）。"""
    t = "活動日：2026-08-17\n次回MS日：2026-08-24\n"
    assert sb._extract_field(t, "活動日") == "2026-08-17"
    assert sb._extract_field(t, "次回MS日") == "2026-08-24"


def test_memo_stops_at_next_label_fullwidth_colon():
    """自由記述欄の終端判定（他ラベル検出）も全角コロンに対応していること。"""
    t = "追記メモ：一行目\n続き\n担当：早瀬\n"
    assert sb._extract_field(t, "追記メモ") == "一行目\n続き"
    assert sb._extract_field(t, "担当") == "早瀬"
