"""名刺xlsx（Eight / CAMCARD等エクスポート形式）からリードを一括取込。

openpyxlはこのファイル形式でFill()エラーを起こすため、
zipから直接XMLを読み込む方式を採用。
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
import xml.etree.ElementTree as ET

from . import sfa_db

logger = logging.getLogger(__name__)

# 名刺xlsxのヘッダ列名と列インデックスのマッピング（0始まり）
# 列の並び（確認済み）:
# 作成時間(0), お名前(1), 苗字(2), 名前(3), 業種(4), 所在地(5),
# 会社名1(6), 部門1(7), 役職1(8), 会社名2(9), 部門2(10), 役職2(11),
# 会社名(その他)(12), 部門(その他)(13), 役職(その他)(14),
# 携帯電話1(15), 携帯電話2(16), 携帯電話(その他)(17),
# 電話番号1(18), 電話番号2(19), 電話番号(その他)(20),
# Fax1(21), Fax2(22), Fax(その他)(23),
# メールアドレス1(24), メールアドレス2(25), メールアドレス(その他)(26),
# 住所1: 国名(27), 都道府県(28), 市(29), 町1(30), 町2(31), 郵便番号(32),
# 住所2: 国名(33), 都道府県(34), 市(35), 町1(36), 町2(37), 郵便番号(38),
# 住所(その他)(39), ウェブページ(40), IM(41), SNSアカウント(42),
# 誕生日(43), 記念日(44), グループ(45), ニックネーム(46),
# メモ1...(47), メモ2(48), メモ3(49)

_COL = {
    "name_full": 1,
    "name_last": 2,
    "name_first": 3,
    "industry": 4,
    "company1": 6,
    "title1": 8,
    "company2": 9,
    "mobile1": 15,
    "phone1": 18,
    "email1": 24,
    "memo1": 47,
    "memo2": 48,
    "memo3": 49,
}


def _read_xlsx_bytes(data: bytes) -> list[list[str]]:
    """xlsxバイト列をzip経由でXML解析し、行×列の2次元リストを返す。

    openpyxlが壊れているファイルでも動作する低レベル実装。
    セルが空の場合は空文字列を返す。
    """
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        # ワークシートを検索
        sheets = [f for f in z.namelist() if "worksheets/sheet" in f]
        if not sheets:
            return []

        # 最初のシートを使用
        with z.open(sheets[0]) as f:
            root = ET.parse(f).getroot()

        # 共有文字列（sharedStrings）を読み込む
        strings: list[str] = []
        ss_files = [f for f in z.namelist() if "sharedStrings" in f]
        if ss_files:
            with z.open(ss_files[0]) as f:
                ss_root = ET.parse(f).getroot()
            for si in ss_root.findall(".//x:si", ns):
                t = si.find(".//x:t", ns)
                strings.append(t.text if t is not None and t.text is not None else "")

        # 行・セルを解析
        rows: list[list[str]] = []
        for row in root.findall(".//x:row", ns):
            cells: list[str] = []
            for c in row.findall("x:c", ns):
                t = c.get("t", "")
                v = c.find("x:v", ns)
                if v is not None and v.text is not None:
                    if t == "s":
                        # 共有文字列インデックス
                        idx = int(v.text)
                        cells.append(strings[idx] if idx < len(strings) else "")
                    else:
                        cells.append(v.text)
                else:
                    cells.append("")
            rows.append(cells)

    return rows


def _cell(row: list[str], col: int) -> str:
    """行から指定列の値を安全に取得し、空白を除去して返す。"""
    if col < len(row):
        return (row[col] or "").strip()
    return ""


def parse_meishi_xlsx(data: bytes) -> list[dict]:
    """xlsxバイト列を受け取り、リードdictのリストを返す。

    各dictは sfa_db.upsert_lead() に渡せる形式。
    キー: name, company, title, email, phone, source, notes, industry

    - お名前 → name（なければ 苗字+名前 を結合）
    - 会社名1 → company（なければ 会社名2）
    - 役職1 → title
    - メールアドレス1 → email
    - 携帯電話1 または 電話番号1 → phone（携帯を優先）
    - 業種 → industry
    - source は常に "exhibition"（名刺 = 展示会等）
    - メモ1+メモ2+メモ3 を改行結合 → notes
    - 空行（会社名・氏名ともに空）はスキップ
    """
    rows = _read_xlsx_bytes(data)
    if not rows:
        return []

    leads: list[dict] = []

    # 1行目はヘッダなのでスキップ
    for row in rows[1:]:
        # 氏名を取得
        name = _cell(row, _COL["name_full"])
        if not name:
            last = _cell(row, _COL["name_last"])
            first = _cell(row, _COL["name_first"])
            name = (last + " " + first).strip()

        # 会社名を取得
        company = _cell(row, _COL["company1"])
        if not company:
            company = _cell(row, _COL["company2"])

        # 氏名・会社名ともに空の行はスキップ
        if not name and not company:
            continue

        # 名前のみで会社名が空の場合は "(未設定)" を補完
        if not company:
            company = "(未設定)"

        # 電話番号（携帯優先）
        phone = _cell(row, _COL["mobile1"]) or _cell(row, _COL["phone1"]) or None

        # メモを結合
        memos = [
            _cell(row, _COL["memo1"]),
            _cell(row, _COL["memo2"]),
            _cell(row, _COL["memo3"]),
        ]
        notes = "\n".join(m for m in memos if m) or None

        leads.append({
            "name": name or "(氏名不明)",
            "company": company,
            "title": _cell(row, _COL["title1"]) or None,
            "email": _cell(row, _COL["email1"]) or None,
            "phone": phone,
            "source": "exhibition",
            "notes": notes,
            "industry": _cell(row, _COL["industry"]) or None,
        })

    return leads


def enrich_with_ai(leads: list[dict]) -> list[dict]:
    """Anthropic APIを使い、各リードのindustry/company_sizeをWebリサーチで補強。

    API未設定時は引数をそのまま返す。
    会社名でweb検索し、業界・企業規模を推定してleadに追記する。
    anthropicライブラリが未インストールの場合はスキップ。

    company_size は sfa_db.COMPANY_SIZES から選択:
    ["500億未満", "1000億未満", "5000億未満", "5000億以上"]
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.info("ANTHROPIC_API_KEY未設定のためAI補強をスキップします")
        return leads

    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        logger.info("anthropicライブラリ未インストールのためAI補強をスキップします")
        return leads

    client = anthropic.Anthropic()

    company_sizes = sfa_db.COMPANY_SIZES
    sizes_str = "・".join(company_sizes)

    enriched: list[dict] = []
    for lead in leads:
        company = lead.get("company", "")
        if not company or company in ("(未設定)", "(氏名不明)"):
            enriched.append(lead)
            continue

        # industry が既に設定されている場合はcompany_sizeのみ調査
        has_industry = bool(lead.get("industry"))

        try:
            prompt = (
                f"会社名「{company}」について調査してください。\n\n"
            )
            if not has_industry:
                prompt += "1. この会社の業種・業界（例：製造業、IT・SaaS、金融、商社、医療など）\n"
            prompt += (
                f"2. この会社の売上規模（次の選択肢から最も近いものを選択: {sizes_str}）\n\n"
                "回答は以下のJSON形式のみで返してください（説明不要）:\n"
                '{"industry": "業界名または null", "company_size": "選択肢のいずれかまたは null"}\n\n'
                "情報が不明な場合はnullを返してください。"
            )

            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=256,
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search",
                }],
                messages=[{"role": "user", "content": prompt}],
            )

            # レスポンスからJSONを抽出
            import json  # noqa: PLC0415
            for block in response.content:
                if hasattr(block, "text") and block.text:
                    text = block.text.strip()
                    # JSONブロックを探す
                    start = text.find("{")
                    end = text.rfind("}") + 1
                    if start >= 0 and end > start:
                        try:
                            result = json.loads(text[start:end])
                            if not has_industry and result.get("industry"):
                                lead = {**lead, "industry": result["industry"]}
                            cs = result.get("company_size")
                            if cs and cs in company_sizes:
                                lead = {**lead, "company_size": cs}
                        except json.JSONDecodeError:
                            pass
                    break

        except Exception as exc:  # noqa: BLE001
            logger.warning("AI補強エラー（%s）: %s", company, exc)

        enriched.append(lead)

    return enriched


def import_meishi_file(con, data: bytes, filename: str) -> tuple[int, int, list[str]]:
    """xlsxバイト列を取込みSFA DBに保存。

    Args:
        con: sqlite3.Connection
        data: xlsxファイルのバイト列
        filename: アップロードされたファイル名（ログ用）

    Returns:
        (追加件数, スキップ件数, エラーメッセージリスト)
    """
    errors: list[str] = []
    ok = 0
    skip = 0

    try:
        leads = parse_meishi_xlsx(data)
    except Exception as exc:  # noqa: BLE001
        logger.exception("名刺xlsxの解析に失敗しました: %s", filename)
        return 0, 0, [f"ファイル解析エラー: {exc}"]

    if not leads:
        return 0, 0, ["有効なデータが見つかりませんでした（ヘッダ行のみ、またはファイルが空）"]

    # AI補強を試みる（失敗しても続行）
    try:
        leads = enrich_with_ai(leads)
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI補強処理でエラーが発生しました（スキップして続行）: %s", exc)

    # DBに保存
    for i, lead in enumerate(leads):
        try:
            sfa_db.upsert_lead(con, **lead)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            skip += 1
            row_num = i + 2  # ヘッダ行分を加算
            msg = f"行{row_num}（{lead.get('name', '不明')} / {lead.get('company', '不明')}）の保存に失敗: {exc}"
            errors.append(msg)
            logger.warning(msg)

    logger.info(
        "名刺取込完了: ファイル=%s, 追加=%d件, スキップ=%d件, エラー=%d件",
        filename, ok, skip, len(errors),
    )
    return ok, skip, errors
