# カレンダー↔SFA突合設計書（#64）

翌日アポSlack自動投稿（`scripts/daily_appt_slack_notify.py`）の誤検知・見逃しを、
メンバーのGoogleカレンダーとSFAの商談日付を突合することで解消する。

本書は**生きた設計記録**。決定事項・検証結果を都度追記し、実装はゲート単位で人の確認を挟んで進める。

- リポジトリはPUBLIC。**認証情報・クライアント名・実データは本書に書かない**。
- SFA本体（`cowork/webapp.py`）はPython標準ライブラリのみだが、`scripts/`配下のcronスクリプトは
  既に`gspread`/`google-auth`等の外部ライブラリを使用しており（Sheets連携）、本機能も同様に
  `scripts/`側で外部ライブラリ利用を許容する（親CLAUDE.mdの「挙動安定・依存ゼロ優先」はwebapp.py本体の話）。

## 1. 決定ログ
- **2026-08-16**: 既存タスク#64（カレンダー↔SFA「アポ」突合チェック）の着手として設計開始。
- 実際の発端: SFA上の`next_milestone_date`が人為的な入力ミス・消し忘れで残っていると、
  実在しない商談が翌朝Slackに誤投稿され、担当者に混乱を与える事象が発生。
- 同時に「実在するのにSFA未登録で投稿されない」という逆方向の実害も確認されており、
  **今回は両方向をまとめて対応する**（ユーザー決定）。
- **カレンダーアクセス方式**: Google Workspace管理者権限による**ドメイン全体の委任
  （service account + domain-wide delegation）**を採用（ユーザー確認: 管理者権限あり）。
  理由: 対象メンバー(11〜12名)全員の予定タイトル・参加者を読む必要があり、Hisho既存の
  個人OAuth方式や、参加者情報を返さないFreeBusy APIでは要件を満たせないため。
- **不一致時の扱い**: SFAに日付があるがカレンダーに外部会議が見当たらない場合、
  **投稿はする（消さない）が「⚠️カレンダー未確認」を付記**（false negative悪化を避ける、ユーザー決定）。
- **逆方向の検知**: カレンダーに外部会議があるがSFAに次回MS未登録の場合も、
  **今回まとめて検知・通知する**（ユーザー決定）。

## 2. ゴール・非スコープ
**ゴール**: 翌朝の#sales投稿を、SFA単独の入力ミスに引きずられず、実態（カレンダー）に近づける。
- 誤投稿（存在しない商談）→ 完全防止は狙わず、**まず注記で警告**（運用を見て強化判断）。
- 見逃し（実在するのに投稿されない）→ カレンダー側から検知して別途フラグする。

**非スコープ（当面）**:
- カレンダー予定とSFA商談の**完全自動マッチング・自動登録**（次回MS日を自動で書き換える等）は行わない。
  あくまで「投稿の確からしさ向上」と「見逃しの検知」に留め、SFAへの書き戻しは人が行う。
- 個人の予定内容（社内会議・休暇等）をSlackに出力しない（外部会議候補のみ扱う）。

## 3. カレンダーアクセス方式
Hisho（秘書Bot）の既存`google_calendar.py`は**早瀬個人のOAuthリフレッシュトークン1本**で動作する
個人秘書実装であり、他メンバーのカレンダーは読めない。`query_freebusy()`で複数人の空き状況は
取れるが、予定タイトル・参加者は返らないため「外部会議かどうか」の判定ができない。

→ 本機能は**新規の専用サービスアカウント**（Google Cloud）を作成し、Google Workspace管理コンソールで
**ドメイン全体の委任（domain-wide delegation）**を設定し、`calendar.readonly`スコープで
各メンバーになりすまして（`with_subject(email)`）予定を読む。既存のSheets用`service_account.json`
（`GOOGLE_SERVICE_ACCOUNT_JSON`）とは**別の認証情報**として扱う（最小権限・用途分離）。

## 4. アーキテクチャ
```
[daily_appt_slack_notify.py]（毎朝06:00 JST）
  ├─ SFA /api/deals から open商談を取得（既存）
  ├─ 対象（next_milestone_date=翌日 かつ タスクでない）を抽出（既存）
  │
  ├─ ①順方向チェック（各対象商談ごと）
  │    owner → owner_slack_map.json → email
  │    → WorkspaceCalendarClient.list_events_for_date(email, 翌日)
  │    → 外部会議判定（is_external_meeting）に該当する予定が1件でもあれば OK
  │    → 無ければ「⚠️カレンダー未確認」を付記して投稿（消さない）
  │
  └─ ②逆方向チェック（全対象メンバー横断・1回）
       owner_slack_map.json の全メンバーについて、翌日の外部会議を列挙
       → 各外部会議ごとに、そのownerのopen商談でnext_milestone_date=翌日のものが
         1件でも無ければ「カレンダーにあるがSFA未登録」として別メッセージで通知
```
- `cowork/workspace_calendar.py`（新規）: サービスアカウント＋委任のカレンダークライアント。
  `CalendarEvent`・`WorkspaceCalendarClient`・`is_external_meeting()`。Hisho`google_calendar.py`の
  データ構造を参考にしつつ、認証方式のみ差し替え（OAuth→サービスアカウント委任）。
- ロジック本体は`scripts/daily_appt_slack_notify.py`に追加（クライアントはinjectableにし、
  fakeクライアントでのユニットテストを可能にする＝実クレデンシャル無しでロジックを検証できる設計）。

## 5. 判定ロジック
### 5.1 「外部会議」の推定基準（`is_external_meeting`）
- 終日予定は除外（休暇・社内イベント表記が多いため）。
- 参加者(attendees)が実質1名以下（自分のみ）の予定は除外（リマインダー/ブロック予定）。
- 参加者メールのドメインが自社ドメイン（既定`inproc.org`）以外を**1件以上**含む予定を「外部会議候補」とする。
- タイトルに明確な除外ワード（例: "休", "OOO", "有給"）を含む場合は除外（簡易フィルタ、必要なら拡張）。

### 5.2 順方向（SFA→カレンダー）
- 対象商談のowner・翌日について、外部会議候補が1件でもあれば「確認OK」、無ければ注記のみ（削除しない）。
- アカウント名と予定タイトル/参加者の突合（＝どの商談の会議か特定）は**v1では行わない**
  （「その日に外部会議が存在するか」の有無チェックに留める。精度向上は運用を見てから)。

### 5.3 逆方向（カレンダー→SFA）
- 各メンバーの翌日の外部会議候補ごとに、そのownerのopen商談でnext_milestone_date=翌日のものが
  SFA側に1件でもあるかを確認。無ければ「未登録の可能性がある会議」として通知。
- 1メンバーが同日に複数の外部会議を持ち、SFA側の登録数より多い場合も、超過分をまとめて通知する
  （個々の会議とSFA商談の1:1紐付けはv1では行わない）。

## 6. 設定・秘匿情報
- `GOOGLE_CALENDAR_SA_JSON`: 新規サービスアカウントの鍵JSON（ファイルパス or JSON文字列、
  `GOOGLE_SERVICE_ACCOUNT_JSON`と同じ二方式対応）。Renderは`sync:false`。
- `GOOGLE_WORKSPACE_DOMAIN`: 自社ドメイン（既定`inproc.org`）。
- 既存`config/owner_slack_map.json`（担当者名→email）をそのまま流用。

## 7. フェーズ計画
- **P1（2026-08-16 実装済み）**: `cowork/workspace_calendar.py`（`CalendarEvent`・
  `is_external_meeting`・`WorkspaceCalendarClient`）＋`daily_appt_slack_notify.py`への
  順方向（`check_owner_has_external_meeting`→注記）/逆方向（`find_unmatched_calendar_meetings`
  →DM通知）組み込み。`GOOGLE_CALENDAR_SA_JSON`未設定（＝P2未着手）の間はカレンダーチェック
  自体を丸ごとスキップし、**現行通りの投稿動作を維持する**（fail-open）。
  テスト: `tests/test_workspace_calendar.py`（判定ロジック7件）・
  `tests/test_calendar_crosscheck_logic.py`（フェイクカレンダークライアントでの突合ロジック5件）。
  `requirements.txt`に`google-api-python-client`追加。`render.yaml`の`sfa-daily-appt-notify`に
  関連環境変数を追加（値はまだ未設定＝P2待ち）。
- **P2（要ユーザー作業・未着手）**: GCPでサービスアカウント作成→Google Workspace管理コンソールで
  ドメイン全体の委任を設定（`calendar.readonly`スコープ）→Renderに`GOOGLE_CALENDAR_SA_JSON`設定。
  手順は[`02_Googleカレンダー委任セットアップ手順.md`](./02_Googleカレンダー委任セットアップ手順.md)。
- **P3（P2完了後）**: 本番で数日運用し、誤検知/見逃しの実績を見て、判定基準（5.1）やv1で見送った
  アカウント単位のマッチング精度改善を検討。

## 8. 決定事項（追加）
- **逆方向通知の投稿先**: #98の見逃し検知リマインドと同じ考え方で、**当面は早瀬個人へのDM固定**
  （新規の未検証ロジックをいきなり#salesに出さない）。`CALENDAR_CROSSCHECK_NOTIFY_MODE=dm|channel`
  で切替可能にする（コード変更不要で#salesへ移行できる）。
  順方向の「⚠️カレンダー未確認」注記は、既存の#sales投稿そのものへの付記なので対象外（そのまま#salesへ出る）。

## 9. 未確定・残課題
- [ ] マッチング精度（アカウント名と会議の紐付け）は当面「有無チェック」のみ。必要なら次段で強化。
- [ ] 除外ワードリスト（休暇表記等）は運用しながら拡充。
</content>
