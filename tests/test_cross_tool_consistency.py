"""SFA-CRM と Hisho の「暗黙の契約」を機械照合するテスト。

2ツールは別リポジトリで、同じ定数・スキーマ・ロジックを人力で二重管理している
（詳細は親フォルダ INTEGRATION.md）。片方だけ変更すると連携がサイレントに壊れるため、
ここで両者の一致をCIレベルで担保する。

Hishoリポジトリは同じ親フォルダ配下の「秘書Bot(Hisho)」に clone されている前提。
見つからない場合はskip（SFA単独の環境でもテスト自体は失敗させない）。
"""
import re
from pathlib import Path

import pytest

from cowork import sfa_db, dev_project_link

# SFAリポジトリルート → 親フォルダ → 秘書Bot(Hisho)
_SFA_ROOT = Path(__file__).resolve().parent.parent
_HISHO_ROOT = _SFA_ROOT.parent / "秘書Bot(Hisho)"
_HISHO_DB_PY = _HISHO_ROOT / "src" / "hisho" / "db.py"
_HISHO_DASHBOARD = _HISHO_ROOT / "dashboard.html"

_hisho_missing = pytest.mark.skipif(
    not _HISHO_DB_PY.exists() or not _HISHO_DASHBOARD.exists(),
    reason="Hishoリポジトリ（秘書Bot(Hisho)）が同じ親フォルダに見つからない",
)


def _hisho_dev_projects_columns() -> set[str]:
    """Hisho db.py の dev_projects CREATE TABLE から列名集合を抽出。"""
    text = _HISHO_DB_PY.read_text(encoding="utf-8")
    m = re.search(r"CREATE TABLE IF NOT EXISTS dev_projects\s*\((.*?)\);", text, re.S)
    assert m, "Hisho db.py に dev_projects テーブル定義が見つからない"
    cols = set()
    for line in m.group(1).splitlines():
        cm = re.match(r"\s*([a-z_]+)\s+[A-Z]", line)
        if cm:
            cols.add(cm.group(1))
    return cols


@_hisho_missing
def test_dev_project_sync_columns_exist_in_hisho():
    """SFAが同期送信する全カラムがHisho側dev_projectsテーブルに存在すること。
    欠けているとINSERT/UPDATEがHisho側で失敗し同期が壊れる。"""
    hisho_cols = _hisho_dev_projects_columns()
    missing = [c for c in dev_project_link.DEV_PROJECT_COLUMNS if c not in hisho_cols]
    assert not missing, f"Hisho dev_projects に存在しない同期カラム: {missing}"


@_hisho_missing
def test_dev_project_statuses_match_dashboard_colors():
    """SFAのDEV_PROJECT_STATUSESが、Hishoダッシュボードの状況色マップDP_STATUS_COLORSの
    キーと一致すること（不一致だとガントの色分けがサイレントに欠落する）。"""
    text = _HISHO_DASHBOARD.read_text(encoding="utf-8")
    m = re.search(r"DP_STATUS_COLORS\s*=\s*\{([^}]*)\}", text)
    assert m, "dashboard.html に DP_STATUS_COLORS が見つからない"
    keys = set(re.findall(r"'([^']+)'\s*:", m.group(1)))
    assert set(sfa_db.DEV_PROJECT_STATUSES) == keys, (
        f"状況の値がズレている SFA={sfa_db.DEV_PROJECT_STATUSES} Hisho={keys}")


@_hisho_missing
def test_dev_project_stages_match_dashboard_abbr():
    """SFAのDEV_PROJECT_STAGESが、Hishoダッシュボードのステージ略称マップDP_STAGE_ABBRの
    キーと一致すること。"""
    text = _HISHO_DASHBOARD.read_text(encoding="utf-8")
    m = re.search(r"DP_STAGE_ABBR\s*=\s*\{([^}]*)\}", text)
    assert m, "dashboard.html に DP_STAGE_ABBR が見つからない"
    keys = set(re.findall(r"'([^']+)'\s*:", m.group(1)))
    assert set(sfa_db.DEV_PROJECT_STAGES) == keys, (
        f"ステージの値がズレている SFA={sfa_db.DEV_PROJECT_STAGES} Hisho={keys}")


@_hisho_missing
def test_dev_period_multipliers_match_dashboard():
    """開発期間のステージ係数（プロト×1/PoC×4/本番×2）がSFA(Python)とHisho(JS)で一致すること。
    dashboard.html の devPeriodDays の mult 定義をパースして突合する。"""
    text = _HISHO_DASHBOARD.read_text(encoding="utf-8")
    m = re.search(r"const mult\s*=\s*stage\s*===\s*'PoC'\s*\?\s*(\d+)\s*:\s*"
                  r"stage\s*===\s*'本番'\s*\?\s*(\d+)\s*:\s*(\d+)", text)
    assert m, "dashboard.html の devPeriodDays 係数定義が見つからない"
    poc_mult, honban_mult, base_mult = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # SFA側の実装から基準日数を逆算して比較（バックエンド無し・難易度なしで基準値）
    base_days = sfa_db.dev_period_days("プロト", "無し", None)
    assert sfa_db.dev_period_days("PoC", "無し", None) == base_days * poc_mult
    assert sfa_db.dev_period_days("本番", "無し", None) == base_days * honban_mult
    assert base_mult == 1  # プロト（else）は×1


@_hisho_missing
def test_jp_fixed_holidays_match_dashboard():
    """HishoダッシュボードのjpHolidaysForYearが持つ固定日祝日が、SFA(Python)側の
    祝日集合にも全て含まれること（固定日の追加/削除の片側もれを検出）。"""
    text = _HISHO_DASHBOARD.read_text(encoding="utf-8")
    m = re.search(r"function jpHolidaysForYear\(year\)\s*\{(.*?)\n\}", text, re.S)
    assert m, "dashboard.html に jpHolidaysForYear が見つからない"
    # add(月, 日) の固定日リテラルのみ抽出（nthMonday/equinox等の計算値は対象外）
    fixed = [(int(mm), int(dd)) for mm, dd in re.findall(r"add\(\s*(\d+)\s*,\s*(\d+)\s*\)", m.group(1))]
    assert fixed, "固定日祝日のadd(m,d)が抽出できなかった"
    from datetime import date
    hol = sfa_db._jp_holidays(2026)
    missing = [(mm, dd) for mm, dd in fixed if date(2026, mm, dd) not in hol]
    assert not missing, f"SFA側の祝日集合に無い固定日祝日: {missing}"
