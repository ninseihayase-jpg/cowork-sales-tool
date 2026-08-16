# 次回MS（マイルストン）ライフサイクル設計書（新規タスク）

商談の「次回MS」が、経路によって上書き/追加/直接キャッシュ書き換えとバラバラに扱われており、
古いMSが「完了」フラグ付けされないまま残って「MS超過」に誤って出てしまう問題を解消する。

本書は**生きた設計記録**。決定事項・検証結果を都度追記し、実装はゲート単位で人の確認を挟んで進める。

- リポジトリはPUBLIC。**認証情報・クライアント名・実データは本書に書かない**。

## 1. 決定ログ
- **2026-08-17**: ユーザーより着手指示。「複数MSのうち完了したものを『完了』と記録するか消すか、
  運用上どちらも存在している」「Slackメモ→SFA更新の経路ですら、次回MSが上書き/追加で混在している
  ように見える」「次回MS追加時に現MSが完了フラグ付けされない可能性があり、MS超過と誤判定される
  不都合が起きうる」との懸念を受け、システム上の実態調査→設計→実装まで行う。
- 調査の結果、**懸念は的中しており、想定より深刻なバグ（Slack Bot経路のキャッシュ完全乖離）も
  発見**（§3）。
- **自動完了の範囲（ユーザー確定）**: 活動履歴を記録した際、その活動日(occurred_on)**以前**の
  未完了MSは**全件**自動完了にする（最古1件だけでなく全件）。

## 2. 現状の仕組み（前提）
`deal_milestones`テーブルが正本、`deals.next_milestone_date/label/type`は「未完了(done=0)で
最も日付の早いMS」を表すキャッシュ（`recompute_deal_next_milestone`が唯一の正しい更新経路、
`sfa_db.py:1817`にコメントあり）。MS超過タブ等の集計は**キャッシュ列のみ**を参照する。

## 3. 調査結果: 経路ごとの挙動と問題

| 経路 | 使用関数 | 挙動 | 判定 |
|---|---|---|---|
| 商談ページ「現状更新」クイックフォーム(2箇所: `webapp.py`旧`/deal/{id}/quick-update`相当) | `upsert_earliest_milestone` | 既存の未完了MSをその場で上書き | 問題なし（旧MSの履歴は残らない） |
| 商談編集フォーム(MS一覧編集) | `set_deal_milestones` | 全行を人が完了チェック付きで置き換え | 人が完了チェックを入れ忘れれば同種の問題が起きうる |
| MSパネルの手動追加/編集/削除(`/deal/{id}/milestones/*`) | `add_deal_milestone`/`update_deal_milestone`/`delete_deal_milestone` | 手動。完了チェックボックス(`data-mf="done"`)あり | 完了操作は人任せ、自動の安全網なし |
| **`/hearing/intake/commit`**（#29取り込みフロー） | `add_deal_milestone` | 新しいMSを**追加するだけ**、旧MSを完了にしない | **バグ**: 旧MSの日付が過去のまま未完了で残り、MS超過に出続ける |
| **Slack Bot（`slack_bot.apply_to_db`）** | `UPDATE deals SET next_milestone_date=...`（生SQL） | `deal_milestones`テーブルを一切触らずキャッシュ列だけ直接上書き | **バグ（最重要）**: テーブルとキャッシュが完全乖離。その後MSパネル操作等で`recompute_deal_next_milestone`が走ると、Slackで正しく更新したはずの次回MSが、テーブルに残っていた古い(未完了)MSへ**静かに巻き戻る** |

また`slack_bot.apply_to_db`は活動履歴の追加も`sfa_db.add_activity()`を経由せず生SQL
`INSERT INTO activities`で行っており、`sfa_db`層の共通処理（後述§4の自動完了含む）が
一切効かない状態だった。

## 4. 設計方針
1. **正本は`deal_milestones`のみ。キャッシュ列(`deals.next_milestone_*`)への直接書き込みは
   全経路で禁止**し、`sfa_db`の関数（内部で`recompute_deal_next_milestone`を呼ぶもの）を
   必ず経由させる。
2. **`sfa_db.add_activity()`に「活動日(occurred_on)以前の未完了MSを自動完了にする」処理を
   組み込む**（新規ヘルパー`complete_past_milestones(con, deal_id, as_of_date)`）。
   活動履歴を記録する＝そのタイミングを持つ全経路（Web/Slack/取り込み）が`add_activity`を
   通る前提にすることで、経路ごとに完了処理を書かずとも一律で正しく動く。
   - 自動完了の範囲: `ms_date <= occurred_on` の未完了MSを**全件**完了にする（§1決定）。
3. **`slack_bot.apply_to_db`を修正**し、活動履歴は`sfa_db.add_activity()`経由に、次回MS更新は
   `sfa_db.add_deal_milestone()`経由に変更する（生SQLを撤廃）。
   - 自動完了（②）が先に効くため、「新しいMSを追加する」だけで、古いMSは既に完了済みになっている
   　→ 経路間の「上書き/追加」の食い違いが実質解消される
   　（`upsert_earliest_milestone`系の経路も、自動完了で未完了MSが無くなれば新規作成＝実質「完了→追加」と同じ結果になる）。
4. **既存データの補正**: 本番DBには既に「本当は完了しているのに未完了のまま残っているMS」が
   存在する可能性がある。導入時に一括検知・確認する運用面のフォローを検討する（§6）。

## 5. 実装対象（2026-08-17 実装済み・push前提でテスト全件パス）
- `sfa_db.py`:
  - `complete_past_milestones(con, deal_id, as_of_date, commit=True) -> int`（完了件数を返す）
  - `add_activity()`に上記を組み込み（`occurred_on`があれば呼ぶ）
- `slack_bot.py`:
  - `apply_to_db`: 活動履歴INSERTを`sfa_db.add_activity()`呼び出しに置換（生SQL撤廃）
  - `apply_to_db`: 次回MS更新（`次回MS日`/`次回MSラベル`/`次回MS種別`）を`sfa_db.add_deal_milestone()`
    呼び出しに置換（`deals`への直接UPDATEから除外）
- テスト: `tests/test_milestone_lifecycle.py`（6件）— 自動完了ロジック単体、Slack Bot経路での
  完了+追加の一貫性（バグ再現→修正確認）、`/hearing/intake/commit`相当フローのMS超過解消（回帰）。
  既存テストへの副作用は無し（全243件パス）。

## 6. 既存データ補正（2026-08-17 実装済み）
`scripts/backfill_stale_milestones.py`を追加。判定ロジックは「各商談の活動履歴
occurred_on最大値」を求め、その日付以前の未完了MSを完了にする（`add_activity`に組み込んだ
自動完了ロジックを過去分に再現するだけなので安全・冪等）。

- 既定は**dry-run**（何も書き込まない・検出結果を表示するのみ）。`--apply`で初めて書き込む。
- `--apply`時は`sfa_db.backup_now`で**適用前に自動バックアップ**を作成してから反映する。
- テスト: `tests/test_backfill_stale_milestones.py`（4件）。スモークテストでdry-run→apply→
  再dry-run(0件・冪等)を確認済み。
- 本番実行はユーザー判断で実施する（`python3 scripts/backfill_stale_milestones.py`→内容確認→
  `--apply`）。

## 7. 未確定・残課題
- [ ] `set_deal_milestones`（商談編集フォームでのMS一覧編集）は人が完了チェックを操作する経路の
      ままにするか、こちらにも自動補正を効かせるか（活動履歴が伴わない編集のため今回は対象外）。
</content>
