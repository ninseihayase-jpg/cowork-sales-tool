"""#101マイルストン2: 早瀬個人のGoogleカレンダーを「今日明日のタスク」画面に重ね表示するための、
一回限りのOAuth認可スクリプト。ローカルPCで1回だけ実行し、表示されたリフレッシュトークンを
RenderのHAYASE_GOOGLE_REFRESH_TOKENに設定する（本体アプリはこのトークンを使い回すだけで、
このスクリプト自体は本番では動かさない）。

事前準備（GCP側。詳細は #142/#101のやり取り参照）:
  1. https://console.cloud.google.com で対象プロジェクトを開く（既存プロジェクトで可）。
  2. 「APIとサービス→ライブラリ」で Google Calendar API を有効化。
  3. 「APIとサービス→OAuth同意画面」で ユーザータイプ=内部 を選択して設定。
  4. 「APIとサービス→認証情報→認証情報を作成→OAuthクライアントID」→
     アプリケーションの種類=デスクトップアプリ で作成し、JSON をダウンロード
     （client_secret*.json。このファイルはGitにコミットしない）。

実行（ローカルPCで。ダウンロードした鍵JSONを使う）:
  pip install google-auth-oauthlib   # このスクリプト専用。本体アプリには不要
  python scripts/gcal_oauth_authorize.py path/to/client_secret_xxx.json

このスクリプトは自動でブラウザを開こうとしない（WSL環境ではブラウザが見つからず
クラッシュするため、open_browser=Falseで固定している）。ターミナルに表示されるURLを
手動でコピーしてブラウザに貼り付け、早瀬個人のGoogleアカウントでログイン・同意すると、
ターミナルにCLIENT_ID / CLIENT_SECRET / REFRESH_TOKENが表示される。この3つをRenderの
sfa-crm（Webサービス）の環境変数 HAYASE_GOOGLE_CLIENT_ID / HAYASE_GOOGLE_CLIENT_SECRET /
HAYASE_GOOGLE_REFRESH_TOKEN にそれぞれ設定する。
"""
from __future__ import annotations

import sys

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def main() -> None:
    if len(sys.argv) != 2:
        print(f"使い方: python {sys.argv[0]} path/to/client_secret_xxx.json", file=sys.stderr)
        sys.exit(1)
    client_secrets_path = sys.argv[1]

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, scopes=CALENDAR_SCOPES)
    # open_browser=False: WSL等ブラウザが見つからない環境ではwebbrowser.open()が例外を投げて
    # スクリプトごと落ちるため、常に手動コピー方式にする（表示されたURLを自分でブラウザに貼る）。
    creds = flow.run_local_server(port=0, open_browser=False)

    print("\n認可に成功しました。以下をRenderの環境変数に設定してください。\n")
    print(f"HAYASE_GOOGLE_CLIENT_ID={creds.client_id}")
    print(f"HAYASE_GOOGLE_CLIENT_SECRET={creds.client_secret}")
    print(f"HAYASE_GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
    if not creds.refresh_token:
        print(
            "\n⚠ refresh_tokenが空でした。以前に同意済みのアプリだと再発行されない場合があります。"
            "\n  Googleアカウントの「サードパーティ製アプリとサービス」設定でこのアプリのアクセスを"
            "\n  いったん削除してから、このスクリプトを再実行してください。",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
