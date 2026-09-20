"""独自アイコンの回帰テスト(2026-09-20〜)。

ユーザー報告: Windows/ChromeでSFA-CRMとHisho dashboard（別リポジトリの姉妹アプリ）を
タスクバーにピン留めすると、どちらも既定の「I」アイコンで見分けが付かない。
当初は仮のインラインSVG(data URI)で対応していたが、後日ユーザーが正式デザインの
アイコンセット（cowork/static/icons/配下、/static/icons/ 経由で配信）を用意したため、
そちらに切り替えた。主要画面（ログイン・メインアプリ・資料閲覧・週次レポート・
マーケ診断ツール）のfavicon、ヘッダーロゴ、/static/icons/配信ルート、/favicon.ico
フォールバックを回帰テストする。

一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import json
import shutil
import tempfile
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


def test_icon_files_exist_on_disk():
    for _fn in ("salesforce.svg", "salesforce.ico", "salesforce-512.png"):
        p = Path(webapp._ICON_DIR) / _fn
        assert p.is_file(), f"{p} が見つかりません"
        assert p.stat().st_size > 0


def test_salesforce_svg_is_valid_and_distinct_from_hisho_delivery():
    svg = (Path(webapp._ICON_DIR) / "salesforce.svg").read_text(encoding="utf-8")
    assert svg.strip().startswith("<svg") and svg.strip().endswith("</svg>")
    # 内製Salesforceは「回る（120度ずつ回転）」（README.md参照）。Hisho/デリバリー管理とは
    # 組み方・配色が異なるアイコンであることの目印としてrotate(120指定を確認する。
    assert "rotate(120" in svg


def test_sfa_favicon_links_point_to_static_icon_files():
    assert '/static/icons/salesforce.svg' in webapp._SFA_FAVICON
    assert '/static/icons/salesforce.ico' in webapp._SFA_FAVICON
    assert '/static/icons/salesforce-512.png' in webapp._SFA_FAVICON


def test_main_page_head_includes_favicon_and_header_logo(con):
    html = webapp.render(webapp.deliveries_page(con)).decode("utf-8")
    assert webapp._SFA_FAVICON in html
    assert webapp._SFA_LOGO_IMG in html


def test_login_page_includes_favicon_and_logo():
    html = webapp.login_page("/deliveries").decode("utf-8")
    assert webapp._SFA_FAVICON in html
    assert webapp._SFA_LOGO_IMG in html


def test_reports_page_includes_favicon(con):
    html = webapp._reports_doc("<p>test</p>", page_title="InProc 営業レポート")
    assert webapp._SFA_FAVICON in html


def test_mktg_sim_page_includes_favicon(con):
    html = webapp.mktg_sim_page(con)
    assert webapp._SFA_FAVICON in html
    assert "__FAVICON_LINK__" not in html  # プレースホルダの置換漏れが無いこと


def test_favicon_links_declare_sizes():
    """2026-09-20: sizes未指定だとChromeの「アプリとしてインストール」がアイコン選定に
    失敗し既定アイコンにフォールバックすることがあったため、明示する。"""
    assert 'sizes="any"' in webapp._SFA_FAVICON
    assert 'sizes="16x16 32x32 48x48"' in webapp._SFA_FAVICON
    assert 'sizes="512x512"' in webapp._SFA_FAVICON


def test_manifest_link_present_and_content_valid():
    assert '<link rel="manifest" href="/manifest.webmanifest">' in webapp._SFA_FAVICON
    manifest = json.loads(webapp._SFA_MANIFEST)
    assert manifest["name"] == "Inproc Salesforce"
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert "any" in sizes and "512x512" in sizes
