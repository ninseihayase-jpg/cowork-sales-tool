# カレンダー突合（#64）Googleセットアップ手順（主の作業）

> 「メンバーのGoogleカレンダーを横断して外部会議を検知する」ための、主の操作手順。
> Google Workspace管理者権限が必要（[`DESIGN.md`](./DESIGN.md) 参照）。所要20〜30分程度。

このサービスアカウントは**対象メンバー全員のカレンダーを読み取れる**強い権限を持つため、
既存のSheets用サービスアカウント（`service_account.json`）とは別に、**専用・最小権限**で作成する。

---

## やること全体像

1. GCPで新規サービスアカウントを作成し、鍵JSONを発行
2. Google Workspace管理コンソールで、そのサービスアカウントに**ドメイン全体の委任**を設定
   （スコープ: `calendar.readonly` のみ）
3. 鍵JSONをRenderの`sfa-daily-appt-notify`cronに`GOOGLE_CALENDAR_SA_JSON`として設定

---

## 1. サービスアカウント作成（GCP）

1. https://console.cloud.google.com で対象プロジェクトを開く（Sheets連携と同じプロジェクトでも、
   分離した新規プロジェクトでもどちらでも可。権限分離の観点では新規プロジェクトが望ましい）。
2. 「APIとサービス → ライブラリ」で **Google Calendar API** を検索し **有効化**。
3. 「APIとサービス → 認証情報 → 認証情報を作成 → サービスアカウント」。
   - 名前例：`calendar-crosscheck`。ロールは不要（ドメイン全体の委任で権限を与えるため）。
4. 作成したサービスアカウントを開く → 「キー」タブ → 「鍵を追加 → 新しい鍵を作成 → JSON」。
   - ダウンロードされた JSON が認証鍵。**このファイルはGitにコミットしない**。
5. サービスアカウントの詳細画面で **「クライアントID」**（数字の羅列）を控える
   （手順2-2で使う。`client_email`とは別物）。

## 2. ドメイン全体の委任を設定（Google Workspace管理コンソール）

**要・超管理者(Super Admin)権限**。

1. https://admin.google.com にアクセス（超管理者アカウントでログイン）。
2. 「セキュリティ → アクセスとデータ管理 → APIの制御」を開く。
3. 「ドメイン全体の委任を管理」→「新しく追加」。
4. 「クライアントID」に手順1-5で控えたクライアントIDを入力。
5. 「OAuthのスコープ」に以下**のみ**を入力（最小権限・読み取り専用）：
   ```
   https://www.googleapis.com/auth/calendar.readonly
   ```
6. 「承認」をクリック。

> ⚠️ ここで許可したスコープの範囲で、このサービスアカウントは**ドメイン内の任意のユーザーの
> カレンダーになりすまして読み取れる**。鍵JSONの管理（Renderのsync:false環境変数以外に
> 置かない・Gitにコミットしない）を徹底すること。

## 3. Renderへの設定

1. Renderダッシュボードで `sfa-daily-appt-notify` cronサービスを開く。
2. Environment（環境変数）で以下を設定：
   - `GOOGLE_CALENDAR_SA_JSON` = 手順1-4でダウンロードした鍵JSONの**中身をそのまま貼り付け**
     （ファイルパスでも動作するが、Renderのcronはローカルファイルを永続配置できないため
     JSON文字列としての貼り付けを推奨。`export_deals_to_sheets.py`の`GOOGLE_SERVICE_ACCOUNT_JSON`
     と同じ二方式対応）。
   - `GOOGLE_WORKSPACE_DOMAIN`（既定`inproc.org`のままでよければ変更不要）
   - `CALENDAR_CROSSCHECK_NOTIFY_MODE`（既定`dm`のままでよければ変更不要。運用を見て`channel`へ）
3. `config/owner_slack_map.json` に、対象メンバー全員の**Google Workspaceのメールアドレス**が
   正しく入っていることを確認する（Slack通知先の逆引きと共用。既に大半は登録済み）。

---

## 動作確認

```bash
# ローカルでファイルパス指定してテスト（本番環境変数は使わない）
GOOGLE_CALENDAR_SA_JSON=path/to/calendar-sa.json \
TARGET_DATE=2026-08-20 \
DEAL_ID=<既存の商談ID> \
python scripts/daily_appt_slack_notify.py
```
- `DEAL_ID`指定時は日付条件を無視して1件だけ投稿するが、カレンダー突合ロジック自体は
  `deal_id_override`指定時はスキップする実装のため、突合の動作確認は本番の日次実行
  （またはコードの`deal_id_override`分岐を一時的に外したローカル実行）で行うこと。
- ログに `[INFO] カレンダー突合: 有効（対象メンバー○名）` が出れば設定成功。
- 「⚠️カレンダー未確認」が付いた投稿が出た場合、担当者のカレンダーに実際に外部会議が
  無いかを確認し、精度に問題があれば `is_external_meeting`（`cowork/workspace_calendar.py`）の
  判定基準を見直す。
</content>
