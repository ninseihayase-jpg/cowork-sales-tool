"""タスクの関連リンク機能(task_links, #127 2026-08-28)の回帰テスト。

ガントチャートのフローティング編集で「進捗を追記」に加えて要望された、URLを貼ると
名前付きのリンクボタンになる機能。一時DBのみ使用。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db, webapp


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_task_links_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def test_schema_has_task_links_table(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(task_links)")}
    assert {"task_id", "url", "label"} <= cols


def test_add_task_link_normalizes_scheme_and_persists_label(con):
    tid = sfa_db.upsert_task(con, title="T")
    lid = sfa_db.add_task_link(con, tid, "example.com/page", "参考ページ")
    links = sfa_db.list_task_links(con, tid)
    assert len(links) == 1
    assert links[0]["id"] == lid
    assert links[0]["url"] == "https://example.com/page"  # スキーム無しはhttps://を補う
    assert links[0]["label"] == "参考ページ"


def test_add_task_link_blank_label_stored_as_none(con):
    tid = sfa_db.upsert_task(con, title="T")
    sfa_db.add_task_link(con, tid, "https://example.com", "  ")
    assert sfa_db.list_task_links(con, tid)[0]["label"] is None


def test_add_task_link_empty_url_returns_none_and_adds_nothing(con):
    tid = sfa_db.upsert_task(con, title="T")
    assert sfa_db.add_task_link(con, tid, "") is None
    assert sfa_db.add_task_link(con, tid, "   ") is None
    assert sfa_db.list_task_links(con, tid) == []


def test_delete_task_link(con):
    tid = sfa_db.upsert_task(con, title="T")
    lid = sfa_db.add_task_link(con, tid, "https://example.com")
    sfa_db.delete_task_link(con, lid)
    assert sfa_db.list_task_links(con, tid) == []


def test_task_links_ordered_by_creation_and_scoped_per_task(con):
    t1 = sfa_db.upsert_task(con, title="T1")
    t2 = sfa_db.upsert_task(con, title="T2")
    sfa_db.add_task_link(con, t1, "https://a.com", "A")
    sfa_db.add_task_link(con, t1, "https://b.com", "B")
    sfa_db.add_task_link(con, t2, "https://c.com", "C")
    links1 = sfa_db.list_task_links(con, t1)
    assert [l["label"] for l in links1] == ["A", "B"]
    assert [l["label"] for l in sfa_db.list_task_links(con, t2)] == ["C"]


def test_deleting_task_cascades_task_links(con):
    tid = sfa_db.upsert_task(con, title="T")
    sfa_db.add_task_link(con, tid, "https://example.com")
    con.execute("DELETE FROM tasks WHERE id=?", (tid,))
    con.commit()
    assert sfa_db.list_task_links(con, tid) == []


def test_list_task_links_map_groups_by_task(con):
    t1 = sfa_db.upsert_task(con, title="T1")
    t2 = sfa_db.upsert_task(con, title="T2")
    t3 = sfa_db.upsert_task(con, title="T3")  # リンク無し
    sfa_db.add_task_link(con, t1, "https://a.com", "A")
    sfa_db.add_task_link(con, t1, "https://b.com", "B")
    sfa_db.add_task_link(con, t2, "https://c.com", "C")

    m = sfa_db.list_task_links_map(con, [t1, t2, t3])
    assert [l["label"] for l in m[t1]] == ["A", "B"]
    assert [l["label"] for l in m[t2]] == ["C"]
    assert t3 not in m


def test_list_task_links_map_empty_ids_returns_empty_dict(con):
    assert sfa_db.list_task_links_map(con, []) == {}


# ── #167: 看板カード（通常/事務タスク）に関連リンク(task_links)も表示する ──

def test_tasks_page_shows_task_links_chip(con):
    tid = sfa_db.upsert_task(con, title="通常タスク", status="未着手")
    sfa_db.add_task_link(con, tid, "https://example.com/doc", "資料")
    html = webapp.tasks_page(con)
    assert "tc-url-chip" in html
    assert "資料" in html
    assert f"tcUrlLinkDelete(" in html
    # 既存のSlackリンク・task_entity_links（🔗関連）ボタンと同時に出ていること（#167本題）
    assert "🔗 リンク追加" in html  # Slack用（既存機能）
    assert "🔗 関連" in html or "✎" in html  # task_entity_links用（既存機能）


def test_tasks_page_shows_add_button_when_no_links(con):
    tid = sfa_db.upsert_task(con, title="通常タスク", status="未着手")
    html = webapp.tasks_page(con)
    assert f"tcUrlLinkAdd({tid})" in html
    assert "📎 リンク追加" in html


def test_desk_tasks_page_shows_task_links_chip(con):
    tid = sfa_db.upsert_task(con, title="事務タスク", is_admin=1, status="未着手")
    sfa_db.add_task_link(con, tid, "https://example.com/doc", "資料")
    html = webapp.desk_tasks_page(con)
    assert "tc-url-chip" in html
    assert "資料" in html
    assert f"tcUrlLinkAdd({tid})" in html


def test_task_url_link_html_falls_back_to_domain_when_no_label(con):
    tid = sfa_db.upsert_task(con, title="T")
    lid = sfa_db.add_task_link(con, tid, "https://www.example.com/path")
    links = sfa_db.list_task_links(con, tid)
    html = webapp._task_url_link_html(tid, links)
    assert "example.com" in html
    assert f'id="tcUrlLink-{lid}"' in html


def test_gantt_popup_embeds_links_and_ui(con):
    """#127: ガントのフローティング編集に、リンク追加UIと進捗追記UIが含まれること。"""
    tid = sfa_db.upsert_task(con, title="T", due_date="2026-09-01", effort_level="軽",
                             assignee="早瀬")
    sfa_db.add_task_link(con, tid, "https://example.com", "参考ページ")
    html = webapp.tasks_gantt_page(con)
    assert '"参考ページ"' in html or "参考ページ" in html
    assert "function gtAddLink" in html and "function gtDeleteLink" in html
    assert "function gtAddNote" in html
    assert "🔗 関連リンク" in html and "📝 進捗を追記" in html
    assert "所要時間(h)" in html  # #127: 工数感とあわせて所要時間も編集できる
    assert "状態<br><select" in html  # #127: 状態も編集できる（従来は読み取り専用表示のみ）
