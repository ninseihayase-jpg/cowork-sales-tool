"""独自faviconの回帰テスト(2026-09-20)。

ユーザー報告: Windows/ChromeでSFA-CRMとHisho dashboard（別リポジトリの姉妹アプリ）を
タスクバーにピン留めすると、どちらも既定の「I」アイコンで見分けが付かない。
SVGをdata URIで埋め込んだ独自faviconを主要な画面（ログイン・メインアプリ・資料閲覧・
週次レポート・マーケ診断ツール）に設定し、タスクバー上でも一目でどちらのアプリか
分かるようにする。

一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import shutil
import tempfile
import urllib.parse
from pathlib import Path

import pytest

from cowork import sfa_db, webapp


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_favicon_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def _favicon_svg_source() -> str:
    """_SFA_FAVICONのdata URIから元のSVG文字列を復元する。"""
    href = webapp._SFA_FAVICON.split('href="', 1)[1].rsplit('"', 1)[0]
    assert href.startswith("data:image/svg+xml,")
    return urllib.parse.unquote(href[len("data:image/svg+xml,"):])


def test_favicon_is_valid_svg_with_distinct_color_from_hisho():
    svg = _favicon_svg_source()
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "#2f6fed" in svg  # SFA-CRM側の配色（Hisho dashboard側は#d97706で別配色）


def test_main_page_head_includes_favicon(con):
    html = webapp.render(webapp.deliveries_page(con)).decode("utf-8")
    assert webapp._SFA_FAVICON in html


def test_login_page_includes_favicon():
    html = webapp.login_page("/deliveries").decode("utf-8")
    assert webapp._SFA_FAVICON in html


def test_reports_page_includes_favicon(con):
    html = webapp._reports_doc("<p>test</p>", page_title="InProc 営業レポート")
    assert webapp._SFA_FAVICON in html


def test_mktg_sim_page_includes_favicon(con):
    html = webapp.mktg_sim_page(con)
    assert webapp._SFA_FAVICON in html
    assert "__FAVICON_LINK__" not in html  # プレースホルダの置換漏れが無いこと
