"""担当者(OWNERS)選択は必ず社員マスタ連動にするための再発防止ガード。

過去、論点の議論メンバー等で「担当」系の選択肢を追加する際に、編集可能な社員マスタ
（get_master_list(con, "owners")）ではなくハードコード定数 sfa_db.OWNERS を直接使い、
管理画面で追加した社員が選べない不具合が繰り返し発生した。

このテストは webapp.py 内の `sfa_db.OWNERS` 直参照が、必ずフォールバック定型
`get_master_list(con, "owners") or list(sfa_db.OWNERS)` の一部としてのみ現れることを保証する。
新たに担当セレクトを追加した際に、素の sfa_db.OWNERS を使うと即座に失敗する。
"""
from __future__ import annotations

import re
from pathlib import Path

WEBAPP = Path(__file__).resolve().parent.parent / "cowork" / "webapp.py"


def test_owners_only_used_as_master_fallback():
    src = WEBAPP.read_text(encoding="utf-8")
    total = len(re.findall(r"sfa_db\.OWNERS", src))
    fallback = len(re.findall(r"or list\(sfa_db\.OWNERS\)", src))
    bare = total - fallback
    assert bare == 0, (
        f"webapp.py に素の sfa_db.OWNERS 参照が {bare} 箇所あります。"
        "担当者の選択肢は必ず sfa_db.get_master_list(con, 'owners') or list(sfa_db.OWNERS) "
        "を使い、社員マスタ連動にしてください（管理画面で追加した社員が反映されるように）。"
    )
