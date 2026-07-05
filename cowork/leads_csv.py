"""リード CSV一括取込。

引き継ぎ元: CRM cowork_CRM/crm-app/src/lib/storage.ts parseContactsCsv
"""

from __future__ import annotations

import csv
import io
import json
import os
import urllib.request

from . import sfa_db
from .csv_utils import normalize_csv_text

_VALID_SOURCES = set(sfa_db.LEAD_SOURCES)
_VALID_STATUSES = set(sfa_db.LEAD_STATUSES)

TEMPLATE_HEADERS = [
    "名前", "会社名", "業界", "企業規模", "役職", "メール", "電話",
    "獲得経路", "ステータス", "ピッチテーマ", "担当者", "メモ",
]

TEMPLATE_EXAMPLE_ROWS = [
    ["田中 太郎", "株式会社サンプル商事", "商社・卸売", "1000億未満", "営業部長",
     "tanaka@example.com", "090-1234-5678", "exhibition", "new", "", "吉江", "展示会で名刺交換"],
]


def parse_leads_csv(csv_text: str, themes_by_name: dict, *, owners=None,
                     industries=None, company_sizes=None) -> list[dict]:
    """CSVテキストをリード辞書リストに変換する。

    themes_by_name: {テーマ名 -> pitch_theme_id} のマッピング
    owners/industries/company_sizes: マスタ選択肢外の値を空欄にフォールバックさせるための現在値。
    未指定時はsfa_db側のデフォルト定数で照合する。
    """
    valid_owners = set(owners or sfa_db.OWNERS)
    valid_industries = set(industries or sfa_db.INDUSTRIES)
    valid_sizes = set(company_sizes or sfa_db.COMPANY_SIZES)

    reader = csv.DictReader(io.StringIO(normalize_csv_text(csv_text)))
    results = []
    try:
        for row in reader:
            name = (row.get("名前") or row.get("name") or "").strip()
            company = (row.get("会社名") or row.get("company") or "").strip()
            if not name or not company:
                continue

            source = (row.get("獲得経路") or row.get("source") or "other").strip()
            if source not in _VALID_SOURCES:
                source = "other"

            status = (row.get("ステータス") or row.get("status") or "new").strip()
            if status not in _VALID_STATUSES:
                status = "new"

            theme_name = (row.get("ピッチテーマ") or row.get("pitch_theme") or "").strip()
            theme_id = themes_by_name.get(theme_name) if theme_name else None

            assigned_to = (row.get("担当者") or row.get("assigned_to") or "").strip()
            if assigned_to not in valid_owners:
                assigned_to = None

            industry = (row.get("業界") or row.get("industry") or "").strip()
            if industry not in valid_industries:
                industry = None

            company_size = (row.get("企業規模") or row.get("company_size") or "").strip()
            if company_size not in valid_sizes:
                company_size = None

            results.append({
                "name": name,
                "company": company,
                "industry": industry,
                "company_size": company_size,
                "title": (row.get("役職") or row.get("title") or "").strip() or None,
                "email": (row.get("メール") or row.get("email") or "").strip() or None,
                "phone": (row.get("電話") or row.get("phone") or "").strip() or None,
                "source": source,
                "pitch_theme_id": theme_id,
                "lead_status": status,
                "notes": (row.get("メモ") or row.get("notes") or "").strip() or None,
                "assigned_to": assigned_to,
                "deal_id": None,
            })
    except csv.Error:
        # 一部の行が壊れていても、それまでに読めた行は取り込む
        pass
    return results


def estimate_companies(companies: list[str], industries: list[str], company_sizes: list[str]) -> dict:
    """会社名リストの業界・企業規模をClaude APIで一括推定。
    Returns {company_name: {"industry": ..., "company_size": ...}}（選択肢外はNone）。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not companies:
        return {}

    prompt = (
        f"以下の会社名のリストについて、業界と企業規模を推定してください。\n"
        f"業界の選択肢: {industries}\n"
        f"企業規模の選択肢: {company_sizes}\n"
        f"会社名リスト: {companies}\n\n"
        f"回答はJSON形式で、会社名をキーとし、値は {{\"industry\": \"...\", \"company_size\": \"...\"}} の形式にしてください。"
        f"選択肢にない場合はnullにしてください。JSONのみ返してください。"
    )
    payload = json.dumps({
        "model": "claude-haiku-4-5",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        text = result["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        raw: dict = json.loads(text.strip())
    except Exception:
        return {}

    valid_industries = set(industries)
    valid_sizes = set(company_sizes)
    out = {}
    for company, est in raw.items():
        if not isinstance(est, dict):
            continue
        ind = est.get("industry")
        sz = est.get("company_size")
        out[company] = {
            "industry": ind if ind in valid_industries else None,
            "company_size": sz if sz in valid_sizes else None,
        }
    return out


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


def import_leads(con, csv_text: str, industries=None, company_sizes=None) -> tuple[int, int]:
    """CSVを取り込み、(成功件数, スキップ件数) を返す。"""
    themes = sfa_db.list_pitch_themes(con)
    themes_by_name = {t["name"]: t["id"] for t in themes}
    owners = sfa_db.get_master_list(con, "owners")
    rows = parse_leads_csv(csv_text, themes_by_name, owners=owners,
                           industries=industries, company_sizes=company_sizes)
    if industries and company_sizes and os.environ.get("ANTHROPIC_API_KEY"):
        rows = _estimate_fields(rows, industries, company_sizes)
    ok = skip = 0
    # 行ごとにcommitすると大量件数（数百〜数千行）で著しく遅くなり、
    # サーバー側のリクエストタイムアウト（例: Renderの502）を招くため、
    # 1件ずつ処理はしつつcommitはループ終了後に1回だけ行う。
    for r in rows:
        try:
            sfa_db.upsert_lead(con, commit=False, **r)
            # アカウント自動追加・補完
            company = r.get("company", "")
            if company:
                sfa_db.upsert_account_merge(
                    con, name=company,
                    industry=r.get("industry"),
                    company_size=r.get("company_size"),
                    commit=False,
                )
            ok += 1
        except Exception:
            skip += 1
    con.commit()
    return ok, skip


def build_template_csv() -> bytes:
    """リード一括取込用テンプレートCSV（サンプル行付き）をバイト列で返す。

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
    owners = sfa_db.get_master_list(con, "owners")
    industries = sfa_db.get_master_list(con, "industries")
    sizes = sfa_db.get_master_list(con, "company_sizes")
    themes = [t["name"] for t in sfa_db.list_pitch_themes(con, active_only=True)]

    source_opts = " / ".join(f"{s}({sfa_db.LEAD_SOURCE_LABELS[s]})" for s in sfa_db.LEAD_SOURCES)
    status_opts = " / ".join(f"{s}({sfa_db.LEAD_STATUS_LABELS[s]})" for s in sfa_db.LEAD_STATUSES)

    return f"""# リードCSV一括取込 入力指示（AI用）

以下のCSVテンプレートに、リードのデータを入力してください。
1行目のヘッダ（列名・列順）は変更しないでください。1行 = 1リードです。

## ヘッダ

```
{",".join(TEMPLATE_HEADERS)}
```

## 列ごとの入力ルール

- **名前**: 必須。
- **会社名**: 必須。空欄の行は取込時に無視される。
- **業界**: 次のいずれかと完全一致させること（一致しないと空欄になる）: {" / ".join(industries)}
- **企業規模**: 次のいずれかと完全一致させること（一致しないと空欄になる）: {" / ".join(sizes)}
- **役職 / メール / 電話**: 自由記述。
- **獲得経路**: 次のいずれかのコードで入力すること（日本語ラベルではなくコードを入れる）: {source_opts}。
  一致しない場合は自動的に `other` になる。
- **ステータス**: 次のいずれかのコードで入力すること（日本語ラベルではなくコードを入れる）: {status_opts}。
  一致しない場合は自動的に `new` になる。
- **ピッチテーマ**: 次のいずれかと完全一致させること（一致しない・空欄なら未設定）: {" / ".join(themes) or "(現在登録なし)"}
- **担当者**: 次のいずれかと完全一致させること（一致しないと空欄になる）: {" / ".join(owners)}
- **メモ**: 自由記述。

## 出力形式のルール

- CSV形式（カンマ区切り、値にカンマを含む場合は `""` で囲む）で出力すること。
- 上記の選択肢一覧に無い値は絶対に作らないこと。分からない・調査できていない項目は空欄のままにすること。
- 上記以外の列を追加したり、列の順番を変えたりしないこと。
"""
