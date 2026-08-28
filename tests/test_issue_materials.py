"""論点の検討材料機能(issue_materials, #128 2026-08-28)の回帰テスト。

社内資料の体系化・層1「検討材料になった情報集」。論点メモ(人が書くUI)とは別に、
調査結果・AIレポート等を貼り付け／ファイルドロップで雑に投げ込むだけの置き場。
一時DBのみ使用。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db, webapp


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_issue_materials_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def _issue(con):
    return sfa_db.upsert_deal_issue(con, deal_id=None, issue="論点X", status="議論中")


def test_schema_has_issue_materials_table(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(issue_materials)")}
    assert {"issue_id", "title", "content", "source_url", "added_by"} <= cols


def test_add_issue_material_persists_all_fields(con):
    iid = _issue(con)
    mid = sfa_db.add_issue_material(
        con, iid, "調査結果の本文...", title="ChatGPT調査メモ",
        source_url="https://chat.example.com/abc", added_by="早瀬")
    materials = sfa_db.list_issue_materials(con, iid)
    assert len(materials) == 1
    m = materials[0]
    assert m["id"] == mid
    assert m["title"] == "ChatGPT調査メモ"
    assert m["content"] == "調査結果の本文..."
    assert m["source_url"] == "https://chat.example.com/abc"
    assert m["added_by"] == "早瀬"


def test_add_issue_material_auto_titles_from_content_first_line(con):
    iid = _issue(con)
    sfa_db.add_issue_material(con, iid, "# 見出し行\n本文がここに続く")
    m = sfa_db.list_issue_materials(con, iid)[0]
    assert m["title"] == "見出し行"  # 先頭行の#を除いた文字列がタイトルに


def test_add_issue_material_blank_content_returns_none_and_adds_nothing(con):
    iid = _issue(con)
    assert sfa_db.add_issue_material(con, iid, "") is None
    assert sfa_db.add_issue_material(con, iid, "   ") is None
    assert sfa_db.list_issue_materials(con, iid) == []


def test_delete_issue_material(con):
    iid = _issue(con)
    mid = sfa_db.add_issue_material(con, iid, "本文")
    sfa_db.delete_issue_material(con, mid)
    assert sfa_db.list_issue_materials(con, iid) == []


def test_issue_materials_scoped_per_issue_and_ordered(con):
    i1 = _issue(con)
    i2 = sfa_db.upsert_deal_issue(con, deal_id=None, issue="論点Y", status="議論中")
    sfa_db.add_issue_material(con, i1, "A", title="A")
    sfa_db.add_issue_material(con, i1, "B", title="B")
    sfa_db.add_issue_material(con, i2, "C", title="C")
    assert [m["title"] for m in sfa_db.list_issue_materials(con, i1)] == ["A", "B"]
    assert [m["title"] for m in sfa_db.list_issue_materials(con, i2)] == ["C"]


def test_deleting_issue_cascades_issue_materials(con):
    iid = _issue(con)
    sfa_db.add_issue_material(con, iid, "本文")
    con.execute("DELETE FROM deal_issues WHERE id=?", (iid,))
    con.commit()
    assert sfa_db.list_issue_materials(con, iid) == []


def test_issue_detail_page_renders_materials_box(con):
    """#128: 論点詳細ページに検討材料ボックスが出て、既存の材料が一覧表示される。"""
    iid = _issue(con)
    sfa_db.add_issue_material(con, iid, "検討材料の本文です", title="材料タイトル",
                              source_url="https://example.com/doc")
    issue = sfa_db.get_deal_issue(con, iid)
    html = webapp.deal_issue_detail_page(con, issue)
    assert "📚 検討材料" in html
    assert "材料タイトル" in html
    assert "検討材料の本文です" in html
    assert 'href="https://example.com/doc"' in html
    assert f'/deal-issue/{iid}/material/add' in html
    assert "function matDrop" in html


def test_issue_detail_page_materials_box_empty_state(con):
    iid = _issue(con)
    issue = sfa_db.get_deal_issue(con, iid)
    html = webapp.deal_issue_detail_page(con, issue)
    assert "まだ検討材料はありません" in html
