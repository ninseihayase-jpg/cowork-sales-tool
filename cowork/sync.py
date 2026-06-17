"""フェーズ1の中核：入力ソース（スプシ/xlsx）→ テーマDB へ UPSERT 同期。

- 既存ID → UPDATE / 新規ID → INSERT（冪等。theme_id をキーに何度実行しても重複しない）
- dry-run でSQLを出すだけにできる（本番DBを汚さず検証可能）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import mapping
from .theme_db import ThemeDBClient

# 秘書プロジェクトのテーマ所有ユーザ（import_sales_xlsx.py と同一）。
DEFAULT_USER_ID = "U087V9M3SDD"


@dataclass
class SyncResult:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)  # dry-run時の説明行

    def summary(self) -> str:
        s = f"INSERT={self.inserted}, UPDATE={self.updated}, SKIP={self.skipped}"
        if self.errors:
            s += f", ERROR={len(self.errors)}"
        return s


def sync_rows(
    rows: list[dict],
    client: ThemeDBClient | None,
    *,
    dry_run: bool = False,
    user_id: str = DEFAULT_USER_ID,
) -> SyncResult:
    """正規化済み行リストをテーマDBへ同期する。

    dry_run=True なら client は None でよい（DBアクセスなし、計画のみ返す）。
    """
    result = SyncResult()
    existing = set() if dry_run else (client.existing_theme_ids() if client else set())

    update_sql = mapping.build_update_sql()
    insert_sql = mapping.build_insert_sql()

    for row in rows:
        try:
            fields = mapping.row_to_theme(row)
        except (ValueError, KeyError) as exc:
            result.skipped += 1
            result.errors.append(f"行スキップ: {exc}")
            continue

        tid = fields["id"]
        is_update = dry_run or (tid in existing)

        if is_update:
            label = (
                f"[UPDATE id={tid}] {fields['title']} "
                f"(分類={fields['category']} Status={fields['status']} Stage={fields['deal_stage']})"
            )
            if dry_run:
                result.planned.append(label)
                result.updated += 1
            else:
                try:
                    client.execute(update_sql, mapping.build_update_params(fields))
                    result.updated += 1
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"UPDATE id={tid} 失敗: {exc}")
        else:
            label = f"[INSERT id={tid}] {fields['title']} (分類={fields['category']})"
            if dry_run:
                result.planned.append(label)
                result.inserted += 1
            else:
                try:
                    client.execute(insert_sql, mapping.build_insert_params(fields, user_id))
                    result.inserted += 1
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"INSERT id={tid} 失敗: {exc}")

    return result
