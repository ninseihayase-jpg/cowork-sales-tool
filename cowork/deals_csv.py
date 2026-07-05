"""商談 CSV一括取込。

1つのCSVテンプレートに「商談」行と「アカウント」行を混在させ、
種別列（省略時は商談名の有無）で仕分けて取り込む。
会社名（アカウント）が未登録の場合は自動でアカウントも作成する
（cowork/leads_csv.py のリード一括取込と同じ仕組み）。
"""

from __future__ import annotations

import csv
import io
import os

from . import sfa_db
from .csv_utils import normalize_csv_text
from .leads_csv import estimate_companies

KIND_DEAL = "deal"
KIND_ACCOUNT = "account"
_KIND_LABEL_DEAL = "商談"
_KIND_LABEL_ACCOUNT = "アカウント"

# CSV行 → sfa_db.upsert_deal() にそのまま渡せるフィールド名
DEAL_CSV_FIELDS = [
    "deal_name", "stage", "business_type_l1", "business_type_l2", "lead_pattern",
    "owner", "value_lumpsum", "value_lumpsum_monthly", "value_recurring",
    "client_budget", "next_milestone_date", "next_milestone_label",
    "importance", "goal", "note",
]

TEMPLATE_HEADERS = [
    "種別", "会社名", "業界", "企業規模", "商談名", "ステージ", "事業種別L1", "事業種別L2",
    "リード経路", "担当者", "単発総額(万円)", "単発月額(万円)", "継続月額(万円)",
    "クライアント予算", "次回MS日", "次回MS内容", "重要度", "ゴール", "メモ",
]

TEMPLATE_EXAMPLE_ROWS = [
    ["商談", "株式会社サンプル商事", "商社・卸売", "1000億未満", "コスト削減提案",
     "初回アポ実施", "コスト削減", "コスト診断(無償)", "Exh.", "吉江",
     "500", "", "", "300万円程度", "2026-08-01", "初回訪問", "高", "コスト15%削減", "展示会で名刺交換"],
    ["アカウント", "株式会社サンプル製作所", "製造業(電機・電子・精密)", "500億未満",
     "", "", "", "", "", "", "", "", "", "", "", "", "", "", "今期は商談化見送り、アカウントのみ登録"],
]


def _to_float(v: str):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def parse_deals_csv(csv_text: str, *, stages=None, biz_l1=None, owners=None, lead_patterns=None,
                     company_sizes=None) -> list[dict]:
    """CSVテキストを商談/アカウント辞書リストに変換する。

    各要素は {"kind": "deal"|"account", "company": ..., ...} の形。
    マスタ選択肢外の値は空欄（None）にフォールバックする（リード一括取込と同様の方針）。
    企業規模はアカウント編集画面がプルダウン（マスタ値限定）のため検証対象。
    業界は自由入力欄のため検証しない。
    """
    valid_stages = set(stages or sfa_db.DEAL_STAGES)
    valid_biz_l1 = set(biz_l1 or sfa_db.BUSINESS_TYPE_L1)
    valid_owners = set(owners or sfa_db.OWNERS)
    valid_patterns = set(lead_patterns or sfa_db.LEAD_PATTERNS)
    valid_sizes = set(company_sizes or sfa_db.COMPANY_SIZES)

    reader = csv.DictReader(io.StringIO(normalize_csv_text(csv_text)))
    results = []
    try:
        for row in reader:
            def g(*keys):
                for k in keys:
                    v = row.get(k)
                    if v:
                        return v.strip()
                return ""

            company = g("会社名", "company")
            if not company:
                continue

            deal_name = g("商談名", "deal_name")
            kind_raw = g("種別", "kind")
            if kind_raw in (_KIND_LABEL_DEAL, "deal"):
                kind = KIND_DEAL
            elif kind_raw in (_KIND_LABEL_ACCOUNT, "account"):
                kind = KIND_ACCOUNT
            else:
                kind = KIND_DEAL if deal_name else KIND_ACCOUNT

            if kind == KIND_DEAL and not deal_name:
                continue  # 商談行なのに商談名が無い場合は取込不可

            stage = g("ステージ", "stage")
            if stage not in valid_stages:
                stage = None
            biz1 = g("事業種別L1", "business_type_l1")
            if biz1 not in valid_biz_l1:
                biz1 = None
            biz2 = g("事業種別L2", "business_type_l2")
            valid_biz2 = set(sfa_db.BUSINESS_TYPE_L2_BY_L1.get(biz1, [])) if biz1 else set()
            if biz2 not in valid_biz2:
                biz2 = None
            owner = g("担当者", "owner")
            if owner not in valid_owners:
                owner = None
            pattern = g("リード経路", "lead_pattern")
            if pattern not in valid_patterns:
                pattern = None
            importance = g("重要度", "importance")
            if importance not in sfa_db.IMPORTANCE_OPTIONS:
                importance = None
            company_size = g("企業規模", "company_size")
            if company_size not in valid_sizes:
                company_size = None

            results.append({
                "kind": kind,
                "company": company,
                "industry": g("業界", "industry") or None,
                "company_size": company_size,
                "deal_name": deal_name or None,
                "stage": stage,
                "business_type_l1": biz1,
                "business_type_l2": biz2,
                "lead_pattern": pattern,
                "owner": owner,
                "value_lumpsum": _to_float(g("単発総額(万円)", "単発総額", "value_lumpsum")),
                "value_lumpsum_monthly": _to_float(g("単発月額(万円)", "単発月額", "value_lumpsum_monthly")),
                "value_recurring": _to_float(g("継続月額(万円)", "継続月額", "value_recurring")),
                "client_budget": g("クライアント予算", "client_budget") or None,
                "next_milestone_date": g("次回MS日", "next_milestone_date") or None,
                "next_milestone_label": g("次回MS内容", "next_milestone_label") or None,
                "importance": importance,
                "goal": g("ゴール", "goal") or None,
                "note": g("メモ", "note") or None,
            })
    except csv.Error:
        # 一部の行が壊れていても、それまでに読めた行は取り込む
        pass
    return results


def _estimate_fields(rows: list[dict], industries: list[str], company_sizes: list[str]) -> list[dict]:
    """業界・企業規模が未入力の行をClaude APIで推定補完する。"""
    need_companies = list({r["company"] for r in rows if not r.get("industry") or not r.get("company_size")})
    if not need_companies:
        return rows
    estimates = estimate_companies(need_companies, industries, company_sizes)
    for r in rows:
        est = estimates.get(r["company"], {})
        if not r.get("industry") and est.get("industry"):
            r["industry"] = est["industry"]
        if not r.get("company_size") and est.get("company_size"):
            r["company_size"] = est["company_size"]
    return rows


def import_deals(con, csv_text: str, industries=None, company_sizes=None) -> tuple[int, int, int]:
    """CSVを取り込み、(商談追加件数, アカウント追加件数, スキップ件数) を返す。"""
    stages = sfa_db.get_master_list(con, "deal_stages")
    biz_l1 = sfa_db.get_master_list(con, "business_type_l1")
    owners = sfa_db.get_master_list(con, "owners")
    patterns = sfa_db.get_master_list(con, "lead_patterns")
    rows = parse_deals_csv(csv_text, stages=stages, biz_l1=biz_l1, owners=owners, lead_patterns=patterns,
                           company_sizes=company_sizes)
    if industries and company_sizes and os.environ.get("ANTHROPIC_API_KEY"):
        rows = _estimate_fields(rows, industries, company_sizes)

    ok_deals = ok_accounts = skip = 0
    # 行ごとにcommitすると大量件数（数千行）で著しく遅くなり、
    # サーバー側のリクエストタイムアウト（例: Renderの502）を招くため、
    # 1件ずつ処理はしつつcommitはループ終了後に1回だけ行う。
    for r in rows:
        try:
            is_new_account = con.execute(
                "SELECT 1 FROM accounts WHERE name=?", (r["company"],)
            ).fetchone() is None
            account_id = sfa_db.upsert_account_merge(
                con, name=r["company"], industry=r.get("industry"), company_size=r.get("company_size"),
                commit=False,
            )
            if is_new_account:
                ok_accounts += 1
            if r["kind"] == KIND_ACCOUNT:
                continue
            deal_fields = {k: r.get(k) for k in DEAL_CSV_FIELDS}
            sfa_db.upsert_deal(con, account_id=account_id, status="open", commit=False, **deal_fields)
            ok_deals += 1
        except Exception:
            skip += 1
    con.commit()
    return ok_deals, ok_accounts, skip


def build_template_csv() -> bytes:
    """商談一括取込用テンプレートCSV（サンプル行付き）をバイト列で返す。

    列構造をシンプルに保つため、選択肢一覧はここには含めない
    （人が読む場合は /deals/import ページの一覧、AIに入力させる場合は
    build_ai_prompt() の指示文を使う）。
    先頭にUTF-8 BOMを付与し、Excelで開いた際の文字化けを防ぐ。
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(TEMPLATE_HEADERS)
    for row in TEMPLATE_EXAMPLE_ROWS:
        writer.writerow(row)
    return ("﻿" + buf.getvalue()).encode("utf-8")


def build_ai_prompt(con) -> str:
    """AIにCSVを代行入力させる際に渡す指示文をMarkdown形式で生成する。

    選択肢はマスタ編集の現在値をそのまま反映する（取込時の検証条件と常に一致させるため）。
    """
    stages = sfa_db.get_master_list(con, "deal_stages")
    biz_l1 = sfa_db.get_master_list(con, "business_type_l1")
    owners = sfa_db.get_master_list(con, "owners")
    patterns = sfa_db.get_master_list(con, "lead_patterns")
    sizes = sfa_db.get_master_list(con, "company_sizes")
    industries = sfa_db.get_master_list(con, "industries")

    biz_l2_lines = "\n".join(
        f"  - **{l1}**: {' / '.join(sfa_db.BUSINESS_TYPE_L2_BY_L1.get(l1, [])) or '(なし)'}"
        for l1 in biz_l1
    )

    return f"""# 商談CSV一括取込 入力指示（AI用）

以下のCSVテンプレートに、商談・アカウントのデータを入力してください。
1行目のヘッダ（列名・列順）は変更しないでください。1行 = 1商談 または 1アカウントです。

## ヘッダ

```
{",".join(TEMPLATE_HEADERS)}
```

## 列ごとの入力ルール

- **種別**: 「商談」または「アカウント」のどちらかを必ず入れる。
- **会社名**: 必須。空欄の行は取込時に無視される。
- **業界**: 自由記述可。分かる範囲で次から選ぶことを推奨: {" / ".join(industries)}
- **企業規模**: 次のいずれかと完全一致させること（一致しないと空欄で取り込まれる）: {" / ".join(sizes)}
- **商談名**: 種別が「商談」の行は必須（無いと取込対象外になる）。
- **ステージ**: 次のいずれかと完全一致: {" / ".join(stages)}
- **事業種別L1**: 次のいずれかと完全一致: {" / ".join(biz_l1)}
- **事業種別L2**: 事業種別L1に対応する値のみ有効（一致しないと空欄になる）。対応表:
{biz_l2_lines}
- **リード経路**: 次のいずれかと完全一致: {" / ".join(patterns)}
- **担当者**: 次のいずれかと完全一致: {" / ".join(owners)}
- **重要度**: {" / ".join(sfa_db.IMPORTANCE_OPTIONS)} のいずれか
- **単発総額(万円) / 単発月額(万円) / 継続月額(万円)**: 数値のみ（万円単位）。不明なら空欄。
- **次回MS日**: YYYY-MM-DD形式。
- **クライアント予算 / 次回MS内容 / ゴール / メモ**: 自由記述。

## 出力形式のルール

- CSV形式（カンマ区切り、値にカンマを含む場合は `""` で囲む）で出力すること。
- 上記の選択肢一覧に無い値は絶対に作らないこと。分からない・調査できていない項目は空欄のままにすること。
- 上記以外の列を追加したり、列の順番を変えたりしないこと。
"""
