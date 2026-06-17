# フェーズ1 Googleセットアップ手順（主の作業）

> 「スプレッドシート手編集 → テーマDB自動反映」を稼働させるための、主の操作手順。
> 所要 15〜20分程度。Claude側は鍵JSONとシートIDを受け取れば残りを実装・接続する。

---

## やること全体像

1. Google スプレッドシートを作る（タブ: **Sales** / **Sales以外**）
2. 初期データを貼り付ける（`seed/seed_Sales.csv` / `seed/seed_Sales以外.csv`）
3. GCPでサービスアカウントを作り、鍵JSONを発行
4. そのサービスアカウントのメールアドレスにスプレッドシートを共有（閲覧者）
5. シートID と 鍵JSON を Claude（or .env）に渡す

---

## 1. スプレッドシート作成

1. https://sheets.google.com で新規スプレッドシートを作成。名前例「営業テーマDB（入力用）」。
2. 下部のシートタブを2つにする：**`Sales`** と **`Sales以外`**。
   - 運用イメージ：`Sales` タブ＝営業メンバー全員が編集 / `Sales以外` タブ＝主のみ編集。
   - （権限の厳密制御は任意。まずは運用ルールとして分けるだけでよい）
3. 営業メンバーに編集権限を付与（共有 → 各メンバーのGoogleアカウントを編集者で追加）。

## 2. 初期データ投入

1. リポジトリで初期データCSVを生成（既に生成済みなら省略）:
   ```bash
   python scripts/seed_sheet.py
   ```
   → `seed/seed_Sales.csv`（32行）, `seed/seed_Sales以外.csv`（26行）が出力される。
2. `seed_Sales.csv` の中身を **Sales** タブのA1セルに貼り付け（スプレッドシートの「ファイル→インポート→アップロード」でも可）。
3. `seed_Sales以外.csv` の中身を **Sales以外** タブに同様に貼り付け。
4. 1行目（ヘッダ）は **絶対に変更しない**（同期スクリプトが列名で判定するため）。`ALL_ID` などプレフィックス付きのまま残す。

## 3. サービスアカウント作成（GCP）

1. https://console.cloud.google.com で適当なプロジェクトを選択（なければ新規作成。例「inproc-sales」）。
2. 「APIとサービス → ライブラリ」で **Google Sheets API** を検索し **有効化**。
3. 「APIとサービス → 認証情報 → 認証情報を作成 → サービスアカウント」。
   - 名前例：`sheet-sync`。ロールは不要（シート共有で権限を与えるため）。
4. 作成したサービスアカウントを開く → 「キー」タブ → 「鍵を追加 → 新しい鍵を作成 → JSON」。
   - ダウンロードされた JSON が認証鍵。**このファイルはGitにコミットしない**（`.gitignore`済み）。
5. JSON内の `client_email`（例 `sheet-sync@inproc-sales.iam.gserviceaccount.com`）を控える。

## 4. スプレッドシートをサービスアカウントに共有

1. スプレッドシートの「共有」を開く。
2. 手順3-5の `client_email` を **閲覧者** で追加。
   - これでサービスアカウントがシートを読めるようになる（同期は読み取りのみ）。

## 5. Claude（or .env）へ受け渡し

- **シートID**：スプレッドシートURLの `/d/` と `/edit` の間の文字列。
  例 `https://docs.google.com/spreadsheets/d/`**`1AbCdEf....`**`/edit` の太字部分。
- **鍵JSON**：リポジトリ直下に `service_account.json` として置く（`.gitignore`済み）。
- `.env` を作成（`.env.example` をコピー）して埋める：
  ```
  THEME_API_URL=https://hisho-ohxe.onrender.com
  THEME_API_TOKEN=<秘書プロジェクトのdebug API token>
  SALES_SHEET_ID=1AbCdEf....
  GOOGLE_SERVICE_ACCOUNT_JSON=service_account.json
  SALES_WORKSHEETS=Sales,Sales以外
  ```

---

## 動作確認

```bash
# まず計画だけ（DBに書かない）
python scripts/sync_cli.py --dry-run

# 問題なければ本同期（テーマDB→ダッシュボードに反映）
python scripts/sync_cli.py
```

スプレッドシートを編集 → `sync_cli.py` 実行 → `https://hisho-ohxe.onrender.com/dashboard` に反映、を確認できればフェーズ1完了。

> 自動実行（定時同期）の設定は `docs/02_自動化とデプロイ.md` を参照。
</content>
