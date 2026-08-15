# ヒアリングAI（音声→構造化）設計書

SFA-CRM #29。面談の音声から高精度に文字起こし→AIで「ヒアリング項目別／全体像／NextStep／メール素案」に
構造化し、人の最終確認を経てSFAに確定する。面談1回を「AI支援のもと完結する1ステップ」にする。

本書は**生きた設計記録**。決定事項・検証結果を都度追記し、実装はゲート単位で人の確認を挟んで進める。

- リポジトリはPUBLIC。**認証情報・クライアント名・金額・音声/文字起こし実データは本書に書かない**。
- SFA本体は**Python標準ライブラリのみ**（`http.server`）・SQLite・フレームワーク不使用（親CLAUDE.md方針）。

---

## 1. 決定ログ（Decision Log）
- **2026-08-07**: 方針=**案J（Jamie採用・精度優先）**を採用。まず案Jを実装し、運用後に別ツール移行を再検討、
  技術的に可能なら**リアルタイム化へ移行**する段階戦略（ユーザー決定）。
- 同日: Jamie API 調査完了（下記§3）。**ストリーミング文字起こしAPIは無し**＝「SFA画面内リアルタイム表示」は
  Jamieでは不可。Jamieは**面談後**にWebhook/RESTで文字起こし・サマリ・タスクを配信する。
- 設計原則: **文字起こしソースを差し替え可能な抽象境界**を設ける（§4）。将来Jamie→ストリーミングSTTへ
  移行しても「取り込み以降（整形・確認・確定）」を再利用でき、移行コストを最小化する。
- **2026-08-07（更新）**: Jamie APIは作成可能だが**月¥9,000（Enterprise相当）＝恒常運用に非現実的**（ユーザー確認）。
  → **早々に別ツール/別方式へ移行する前提**に変更。方針転換: **Jamie固有のWebhook連携を先に作り込まない**。
  代わりに **P1＝ソース非依存の取り込み（文字起こしの貼付/アップロード）** を実装し、API費ゼロで最終形の価値
  （整形→確認→確定→メール素案）を先に完成させる。Jamieは無料〜通常プランで録音・文字起こしし、結果を
  手動で貼って“トライ”する。自動取り込みアダプタ（Jamie webhook / 他ツールAPI / Whisper等）は、費用に見合う
  ソースが決まってから `TranscriptSource` の裏に薄く足す（整形〜確定は再利用）。
  移行先候補（P5で技術検証）: 自前/APIのWhisper（~$0.006/分級）、tl;dv/Fireflies/Notta等の安価API、Recall.ai(会議ボット)。

## 2. ゴール（最終形）と非スコープ
最終形フロー（案J）:
```
[Jamieアプリで面談を録音]  ← Zoom/Teams/Meet いずれも端末音声を取得。リアルタイム表示/編集はJamie側
      │ 面談終了→Jamie処理完了
      ▼
[SFA] Webhook受信（署名検証）→ REST APIで 文字起こし＋サマリ＋タスク＋タグ を取得
      ▼
[SFA] Claude整形: ①ヒアリング項目別への割り当て ②面談全体像 ③NextStep案 ④宛先メール素案
      ▼
[SFA] 人の最終確認（項目・NextStepを編集/確定）
      ▼
[SFA確定] hearing_results へ保存＋活動履歴化／NextStep→次回MS・タスク／メール素案→ドラフト
```
- 非スコープ（当面）: SFA画面内でのリアルタイム文字起こし表示・その場編集（＝案R。将来のリアルタイム化で再検討）。
- 利用WebMTG頻度: Zoom > Teams > Google Meet。Jamieは端末音声取得のため全プラットフォーム共通で動作。

## 3. Jamie API 調査結果（ゲート#1）
出典: https://docs.meetjamie.ai/enterprise/admins/integrations-and-developers ／ https://www.meetjamie.ai/
- **REST API**: base `https://beta-api.meetjamie.ai`、認証は `x-api-key` ヘッダのみ（OAuth不要）。
  ワークスペースキー `/v1/workspace/…` と個人キー `/v1/me/…`。取得可能=サマリ/文字起こし/タスク/タグ。
- **Webhook**: 「会議の処理が完了したとき」に発火（**post-meeting**）。署名検証・再送/配信管理あり。
- **MCP**: `https://mcp.meetjamie.ai/mcp`（ブラウザOAuth）。meeting一覧/取得/検索・タスク・テンプレ操作。
- **ストリーミング/WebSocket 文字起こしは無し**（→ 案Jの前提）。
- API/Webhookは上位（Enterprise想定）プラン機能との記述あり → **契約でAPIキーを作成できるか要確認**（未確定・§8）。
- 補足: JamieはSalesforce製品への公式コネクタを持つが、本SFAは自社ツールのため**REST/Webhookで自前連携**する。

## 4. アーキテクチャと抽象境界（将来のリアルタイム移行を見据える）
「文字起こしの入手方法」を **TranscriptSource（取り込み層）** として分離し、下流を共通化する。

```
TranscriptSource（差し替え可能）
  ├─ 案J: JamieSource … Webhook受信 + REST取得（post-meeting・バッチ）           ← 今回実装
  └─ 案R: StreamingSTTSource … ブラウザ音声→STream STT（リアルタイム・将来）      ← 将来追加
        ↓ 共通インターフェイス: {meeting_id, transcript(text/segments), raw_summary?, tasks?, source, occurred_on}
[Intake] 正規化して session として保存（生文字起こし＋メタ）
        ↓
[Structuring] Claude整形（ヒアリング項目別 / 全体像 / NextStep / メール素案）
        ↓
[Review UI] 人の確認・編集・確定
        ↓
[Commit] hearing_results / 活動履歴 / 次回MS・タスク / メールドラフト
```
- 下流（Intake以降）はソース非依存。将来 StreamingSTTSource を足せば、Review/Commitはそのまま使える。
- SFAは標準ライブラリのみ。案Jは**HTTP（Webhook受信＋REST GET）**のみで実現でき、WebSocket中継不要＝素直。
  案R（リアルタイム）は音声ストリーミングが絡み難所（別途PoCで見極め）。

## 5. データモデル（案）
既存を活用: `hearing_templates`（設問）/ `hearing_results`（確定結果）/ `hearing_drafts`（自動保存）。
新規（詳細はゲート#4で確定）:
- `hearing_sessions`: 面談セッション（deal_id, source['jamie'|...], external_id=Jamie meeting id, transcript, raw_summary, status['imported'|'structured'|'confirmed'], occurred_on, created_at）。
- 構造化結果はまず `hearing_sessions` に保持し、人が確定したら `hearing_results`（＋活動履歴）へ反映。
- Webhook冪等化: external_id＋eventで重複取り込みを防ぐ（Slack起票の `_mark_event_processed` と同方式）。

## 6. 技術要素と検証ゲート
| # | 要素 | 検証（PoC）内容 | 状態 |
|---|---|---|---|
| 1 | Jamie API可否 | REST/Webhook/MCPの有無・streaming有無・認証 | ✅ 完了（§3）。streaming無し＝案J |
| 1b| Jamie APIキー/プラン | 契約でAPIキー作成可か（Settings→Developers→API Keys） | ⏳ ユーザー確認待ち |
| 2 | Webhook受信 | SFAに `/api/jamie/webhook`（署名検証・冪等）を実装しJamie処理完了を受信 | 未 |
| 3 | REST取得 | `x-api-key`で meeting の transcript/summary/tasks を取得（薄いHTTPクライアント） | 未 |
| 4 | データ/確定フロー | hearing_sessions 追加、確定→hearing_results＋活動履歴＋NextStep化 | ✅ P1 |
| 5 | Claude整形 | 既存 `_call_claude_haiku` 拡張。項目別/全体像/NextStep/メール素案 | ✅ P1 |
| 6 | 確認UI | 取り込み→整形結果を人が編集・確定する画面（既存hearing UIを拡張） | ✅ P1 |
| 7 | メール素案→ドラフト | 生成メールをOutlookドラフト等へ（#65と連携余地） | 未 |
| 8 | 秘匿/コンプラ | APIキーはRender秘匿、録音同意運用、EUデータresidency前提、公開リポジトリ配慮 | 継続 |

## 7. フェーズ計画（各フェーズで人の確認→確定）※2026-08-07 方針転換を反映
- **P0**: 本設計書作成。方針・調査結果を記録。✅
- **P1（新）**: **ソース非依存の取り込み＋整形＋確認＋確定**（ゲート4,5,6）。✅ 2026-08-13 実装・本番反映（a47d527）。
  文字起こしを**貼付**→Claude(Haiku)整形（項目別/全体像/NextStep/メール素案）→人が確認・編集→
  hearing_sessions保存→確定でhearing_results＋活動履歴＋NextStep（任意でタスク起票）化。API費ゼロで最終形の価値を実証。
  `TranscriptSource` は `PasteSource`（貼付）から開始。
  - 実装: `sfa_db.hearing_sessions`（+CRUD）／`webapp._structure_hearing_transcript`・`hearing_intake_page`・`hearing_review_page`。
  - ルート: `GET /hearing/intake`, `POST /hearing/intake/structure`, `POST /hearing/intake/commit`。商談ページに導線。
  - 残: アップロード（.txt/.vtt）取り込み、未ヒア項目の可視化強化はP1.1で追補。
- **P2**: メール素案→ドラフト（ゲート7）。
- **P3（旧P1相当・保留）**: 自動取り込みアダプタ。費用に見合うソース確定後に `TranscriptSource` の裏へ薄く追加
  （Jamie webhook＝月¥9,000のため保留 / 他ツールAPI / Whisper等）。
- **P4**: リアルタイム化の再検討（案R）。ストリーミングSTT/会議ボットAPIの技術検証→可能なら StreamingSTTSource を追加。

## 8. 未確定・次アクション
- [ ] **ユーザー**: Jamie の Settings→Developers→API Keys でキー作成可否＆プランを確認（ゲート1b）。→ §9で更新：**Proプランで可**。
- [ ] キー確保後、Render(sfa-crm)の秘匿設定に `JAMIE_API_KEY`（またはWebhook用シークレット）を格納（値は非コミット）。
- [ ] Webhook受信URL（`/api/jamie/webhook`）と署名検証仕様をDocsで確認しP3着手。

## 9. 自動連携（P3）設計 — 2026-08-14 調査反映（Zoom / Jamie）

「Zoom or Jamie の文字起こしから自動連携」を検討。結論: **Jamie webhook を第一候補**、Zoom-native は将来の代替/補完。

### 9.1 Jamie（第一候補・調査結果）
- **Webhook `meeting.completed`**（会議処理完了時に発火）。ペイロードに **文字起こし全文・summary・tasks・participants・カレンダー情報（event.attendees=招待メール, event.externalId）が inline** で載る → **追加GET不要**でSFAだけで完結。
- 検証: `x-jamie-signature`（HMAC-SHA256, `{ts}.{body}`, 5分以内）または `x-jamie-api-key` ヘッダ。リトライ5回（即時/+10s/+60s/+10m/+1h）、30sタイムアウト、失敗はDLQ＋管理者メール。
- REST（バックフィル用）: base `https://beta-api.meetjamie.ai`、`x-api-key`、`GET /v1/{workspace|me}/meetings.list|.get`。OpenAPI: `docs.meetjamie.ai/api-reference/openapi.json`（実装前にスキーマ確定）。
- **プラン: Pro（~€47 ≈ ¥7,500-8,000/月）でAPI/Webhook可**（Enterprise専用ではない）。当初「¥9,000でEnterprise相当＝非現実的」としていた前提を**訂正**（§1決定ログの保留理由は解消）。
- 精度: 端末音声を取得しZoom/Teams/Meet横断で動作。日本語精度は本ツール選定の主因（Zoomより良好）。
- データ所在: 独=フランクフルト保存（GDPR、学習非利用、音声は処理後削除）。**日本/APAC residencyは無し**（EU保管を許容できるか要確認）。ストリーミングAPIは無し（post-meetingのみ）。レート100req/分。

### 9.2 Zoom（代替・調査結果）
- `recording.completed` では**文字起こしが未生成のことが多い** → `recording.transcript_completed`（発火が遅延/不安定との報告）で `file_type:"TRANSCRIPT"` のVTTを `download_url`＋`download_token` で取得。フォールバックにポーリング。
- 前提: **Pro+ でクラウド録画＋「音声トランスクリプト」設定ON**（会議前に）。ホストがクラウド録画すること。
- アプリ: **Server-to-Server OAuth の内部アプリ（Marketplace審査不要）**、scope `recording:read`（/ `cloud_recording:read:...:admin`）。Webhookは CRC検証（3秒以内, plainTokenのHMAC）＋`x-zm-signature`。
- **日本語精度が弱く、カスタム辞書も無い**（固有名詞・専門用語で誤変換増）→ 精度優先の本用途では難点。
- マッチングは host+start_time+topic が主（参加者メールは欠落しがち）。AI Companion要約APIは `meeting:read:summary:master` 等のscope取得に難あり（不確実）。

### 9.3 比較と推奨
| 観点 | Jamie | Zoom |
|---|---|---|
| 取得容易性 | ◎ Webhookに全文inline | △ transcript_completed待ち＋DL |
| 日本語精度 | ◎（選定理由） | △ 弱い・辞書なし |
| 会議横断 | ◎ Zoom/Teams/Meet | ✕ Zoom限定 |
| マッチング材料 | ◎ 招待メール/カレンダーID | △ host+時刻+題名 |
| 費用 | Pro ¥7.5-8k/月（契約済枠内） | 既存Zoom+録画/文字起こし前提 |
| データ所在 | EU(独) | 契約リージョン |
→ **Jamie webhook 採用を推奨**。ZoomはJamieがボット参加で録るため実質カバーされ、Zoom-nativeは「Jamie未使用の会議も拾う」補完として将来検討。

### 9.4 SFA側アーキテクチャ（ベンダー非依存の核）
```
[Jamie] meeting.completed webhook → [SFA] POST /api/jamie/webhook（署名検証・冪等: external_id=jamie meeting id）
      → intake_transcripts に生データ保存（kind='inbox' で未割り当て）＋ 会議メタ(タイトル/日時/attendees)を保持
      → [取り込みインボックス] 画面に「未割り当ての取り込み」として表示。候補（attendeeメール→アカウント→進行中商談 / 題名あいまい一致）を提示
      → 人が 商談 or 論点 を選択（＝割り当て）
      → 既存の整形→確認→確定（P1）へ合流（下流は完全再利用）
```
- 冪等化: `intake_transcripts` に `external_source`/`external_id` 列を追加（同一 meeting id の二重受信を無視）。既存の Slack `_mark_event_processed` と同方式。
- **マッチングは自動確定せず「候補提示＋人が割り当て」**から開始（誤紐づけ事故を避ける）。高信頼（単一の明確な商談）時のみ既定選択をプリセット。
- 秘匿情報（APIキー/Webhookシークレット）は Render 環境変数。公開リポジトリにコミットしない。

### 9.5 段階計画（P3）
- **P3.0＋P3.1**: ✅ 2026-08-14 実装・本番反映。Jamie `meeting.completed` 受信（`POST /api/jamie/webhook`・署名/APIキー検証・冪等）
  → `intake_transcripts`（status='inbox'）保存 → `/intake-inbox` 未割り当て一覧（本文参照/破棄）→ 商談/論点へ**割り当て**→既存P1整形（確認→確定）へ合流。題名一致の候補サジェスト付き。ナビに「📥 取り込み」。
  - 実装: `sfa_db.add_inbox_transcript/list_inbox_transcripts/get_intake_by_external/assign_inbox_transcript`／
    `webapp._handle_jamie_webhook・_verify_jamie_request・_jamie_transcript_text・_jamie_attendees・intake_inbox_page`。
  - 秘匿: `JAMIE_WEBHOOK_SECRET`（署名HMAC）または `JAMIE_WEBHOOK_API_KEY`（x-jamie-api-keyヘッダ）。両未設定は fail-closed（503）。render.yaml に sync:false で宣言。
  - 受信URL（本番）: `https://sfa-crm.onrender.com/api/jamie/webhook`（basic認証は /api/* 除外で通過）。
  - **2026-08-15 本番疎通OK**（Jamie Send Test → `saved inbox`）。ハマりどころ＝**認証キーの不一致**:
    JamieのWebhook作成時に付けた**カスタムヘッダ `x-jamie-api-key`（手入力値）が優先**して送られ、
    Jamie自動生成の `sk_…` は送られなかった。→ Render `JAMIE_WEBHOOK_API_KEY` は
    **「Jamieが実際に送っている値」**（カスタムヘッダ値 or Manage API Keyの`sk_`のどちらか実送信される方）に一致させること。
    切り分けは 401ログの `incoming ... fingerprint=[…]` と `Render ... fingerprint=…`（先頭4/末尾2/len）で判定。
    受信/401/400は必ずログに出る（`_handle_jamie_webhook`）。
  - 注意: Jamieの Send Test ペイロードは `id` を持たないため external_id=None（冪等キー無し）。実会議はidありで冪等。
- **P3.2**: 高信頼オートマッチ（プリセットのみ・確定は人）。REST backfill（取りこぼし補完）。
- **P3.3（任意）**: Zoom-native アダプタを同じ受信層の裏に追加（Jamie未使用会議の補完）。

### 9.6 決定事項（2026-08-14 ユーザー確定）
- **連携先=両方対応**。受信層を共通化し、**Jamieを先に実装 → 後からZoom-nativeを追加**（同じ受信/インボックス/整形の下流を共有）。
- **マッチング=インボックス＋候補提示**（自動確定はしない。招待メール→アカウント→進行中商談／題名あいまい一致で候補提示、人が割り当て）。
- 残確認（ユーザー作業）: Jamie Pro＋API/Webhook有効化、EUデータ保管の許容、Render秘匿設定への鍵/シークレット格納。

### 9.7 受信層の共通設計（両ソース共通）
- 抽象境界: `TranscriptSource` の実体として **JamieSource / ZoomSource** を受信アダプタとして追加。両者は差異を吸収し、共通の正規化オブジェクト `{source, external_id, title, occurred_on, transcript, raw_summary?, attendees[], calendar_event_id?}` を出力。
- 保存: `intake_transcripts` を受信インボックスとして拡張（`external_source`,`external_id`,`title`,`occurred_on`,`attendees_json`,`status`（inbox/assigned/done）,`kind`/`entity_id`はNULL可＝未割り当て）。冪等キー=(external_source, external_id)。
- ルート（共通/個別）: `POST /api/jamie/webhook`（署名: x-jamie-signature or x-jamie-api-key）／将来 `POST /api/zoom/webhook`（CRC＋x-zm-signature、transcript_completedでVTT取得）。受信後の処理（正規化→インボックス保存→候補算出）は共通関数へ。
- インボックスUI: `/intake-inbox`（未割り当て一覧＋候補サジェスト＋「商談/論点へ割り当て」）。割り当て後は既存P1整形（/…/intake/structure 相当）へ合流。

## 10. Slackメモ×Jamie取り込みの並行運用（P4設計・2026-08-15）

### 10.1 現状（実コード）
- **商談取り込み（Web, #29再設計・実装済み）**: 文字起こし→オプション選択（活動種別/相手・ヒアリング起票+テンプレ・現状更新[ステージ/次回MS/現状メモ]・メール要否）→AI整形→確認→**セット記録**（活動履歴が土台＋任意でヒアリング/現状更新/メール）。Jamie自動連携は `/intake-inbox` 経由でこの確認画面に合流。
- **Slack NegoCollection（既存）**: #salesスレッドにメモ→`@NegoCollection`→商談推定（deal_name/account部分一致＋人手フォールバック）→Claude Haikuで**更新ドラフト**（activity_date/type, contact, content, stage_update, next_MS, memo_addition）をスレッド投稿→人が「フィールド:値」で修正→**「確定」返信でSFA書き込み**（`activities`追加＋`deals`更新 or 新規商談作成）→Hisho同期。状態は `slack_threads`（identifying→pending→completed）。**activity/deal更新は生SQL**（Web経路と二重化）。
- 両者とも出力は「正しい商談記録＝1活動履歴（±ステージ/次回MS/現状メモ）」で**同型**。ただし別経路・別実装。

### 10.2 並行運用の問題
同一面談で「Jamie文字起こし」と「Slackメモ」が高頻度で同時発生する。素朴に両方処理すると:
1. **活動履歴が二重**に作られる（Jamie取り込み＋Slack確定）。
2. **ステージ/次回MS/現状メモが二重更新・競合**（後勝ちで上書き）。
3. **人が2回確認**する二度手間。
4. 実際は**相補的**（Jamie=全文の記録、Slackメモ=人が強調した要点/決定）で、捨てるのは惜しい。

### 10.3 設計原則
**1面談 = 1商談記録（活動履歴が土台）。Jamie文字起こしとSlackメモは、その1記録に収束する2つの入力。人の最終確認は1回。**

### 10.4 収束のキー（重複判定）
- **(deal_id, occurred_on[面談日])** を「同一面談」の突合キーとする（＋任意でJamie attendees/title と Slackスレッドの近接時刻）。
- 同一キーの記録が既にあれば**新規作成せず統合**（Jamie=全文→活動内容・ヒアリング、Slackメモ=人の強調→上書き/追記）。

### 10.5 スコープの確定（2026-08-15）— Slackメモは「商談×顧客面談」のみ
**Slackメモが発生するのは商談の顧客面談のみ。論点・商談の内部議論はSlackを一切経由しない**（#96のJamie社内議論→商談メモ、および論点側の取り込みは既に#96/#97でWeb完結・実装済みで、本節の対象外）。
→ #98の「突合」設計が必要なのは **商談×顧客面談** の1ケースだけに絞られる。

### 10.6 フローチャート検証で判明した破綻点（2026-08-15）
Slack有無×Jamie先攻/後攻の全パターンを検証した結果、**唯一かつ最大の破綻点は「Jamie文字起こしの商談ひも付け（識別）」**だった。
- Jamie webhook到着時点では `intake_transcripts` に商談との紐付けが一切ない（タイトル/参加者/時刻のみ）。
- 現状この識別は **Web `/intake-inbox` で人が手動で候補チップを押す**しか手段がない（#97実装分）。
- 実運用では**Slackメモの方が先に確定するのが常態**（面談直後にすぐ担当者がSlackに打つため）。Slackで確定した瞬間「もう記録した」という認識になり、**誰もWebのinboxを見に行かなくなる**。
- 未割当のJamie文字起こしを催促するリマインダーも存在しない（コード上確認済み）。
- 結果、Jamie全文が**恒久的に迷子**になり、「相補的に統合する」という設計そのものが機能しない。

なお、Jamie先攻（Slackメモがまだ無い状態でJamieが先に届く）ケースは、識別さえ済めば#97のWeb確認画面にそのまま合流でき、これは既に成立している。破綻するのは常に「識別①がSlack確定より遅れる／起きない」ケースのみ。

### 10.7 確定した解決運用（2026-08-15 ユーザー確定）
**識別①をSlackに引き取らせる。** Jamie webhookが届いた時点で、Botが即座に `#sales` に「Jamie文字起こし到着：[タイトル/時刻/参加者] → どの商談ですか？」を投稿し、既存NegoCollectionの商談推定（テキスト一致＋人手フォールバック）と同じ発想の候補ボタンで人に選ばせる。Webのinboxへの動線切れを解消する。

識別①が完了した時点で、同一(deal_id, 面談日)で**既にSlack確定済みの活動があるか**をBotが即チェックする：
- あれば「この活動［リンク］にJamie全文を追記しますか？」と提示（Slack先攻ケースの救済）。
- なければ、通常通りSlackでの`@NegoCollection`確定を待ち、確定時に同キーのJamie全文があれば統合（Jamie先攻ケース、10.4の優先ルール＝Jamie全文を本文、Slackメモを強調として追記）。

これにより先攻・後攻どちらの順序でも同じロジック（識別①→即時チェック→ない場合は確定時に統合）で吸収でき、フローチャート上の破綻点が解消される。

**スコープ外混入への逃げ道（2026-08-15 追記・要実装）**: Jamie webhookは顧客面談／社内議論／論点関連の区別なく届くため、Botが無差別に商談候補を投稿すると、論点・内部議論の文字起こしまでSlackに紛れ込み、10.5で確定した「論点・内部議論はSlackを経由しない」というスコープを崩しかねない。そのため候補提示には必ず**「対象外（論点／社内議論など）」ボタン**を用意し、押された場合（または一定時間反応がない場合）はSlack側で何もせず、従来通り `intake_transcripts` に `status='inbox'` のまま残す＝**Web `/intake-inbox` 経由の既存フロー（#96/#97実装済み・論点/内部議論とも無改修）にそのまま委ねる**。これにより論点・内部議論の既存コードには一切手を入れず共存できる。

### 10.8 統合受信レイヤー
- Slackメモも `intake_transcripts`（受信インボックス）に「source='slack'」で載せ、Jamieと同じ「未確定の記録候補」として一元管理・突合できるようにする。
- ただし人が日常的に見るUIはSlack。Web `/intake-inbox` は「識別①が漏れて残ったもの」の保険的な可視化ビューとして残す。
- 冪等/状態は既存の `slack_threads`・`slack_processed_events`・`intake_transcripts.status` を流用。

### 10.9 決定事項（2026-08-15 確定）
- [x] スコープ＝**商談×顧客面談のみ**（論点・内部議論はSlackを経由しないため対象外）。
- [x] 最終確認の主導面＝**Slack**。ヒアリングテンプレ起票／重いステージ変更など「丁寧に残したい」時のみWeb確認画面（#29再設計済み）へ格上げ。
- [x] Jamie識別①＝**Slackに移設**。webhook到着時にBotが商談候補を`#sales`に投稿し、人が選ぶ。
- [x] 突合キー＝**(deal_id, 面談日)**。識別①完了時に即座に同キーの確定済み活動をチェックする。
- [x] 統合時の優先＝**Jamie全文を本文、Slackメモを人の強調として上書き/追記**。
- [x] スコープ外混入対策＝候補提示に**「対象外（論点/社内議論など）」ボタン**を用意し、押下時/無反応時は`status='inbox'`のままWebの既存フローに委ねる（論点・内部議論のコードは無改修で共存）。

### 10.10 実装状況（2026-08-15）
コア部分（識別①のSlack移設＋突合・統合）を実装・テスト済み（`tests/test_jamie_slack_identify.py`）。
- `slack_bot.post_jamie_candidate_prompt` — Jamie webhook到着時、商談候補（`webapp._inbox_candidates`を流用、論点は除外）があれば`#sales`（`SALES_CHANNEL_ID`）に候補ボタン＋「対象外」ボタンを投稿。候補ゼロなら投稿自体をスキップ（Web `/intake-inbox` に委ねる）。
- `slack_bot.handle_interactive` — `jamie_pick_deal`（割当→既存活動チェック→無ければ確定待ち／あれば追記提案）・`jamie_skip`・`jamie_append_yes`・`jamie_append_no`を処理。`webapp.py`の`/slack/interactive`はaction_idが`jamie_`始まりならこちらへ、それ以外は従来通り`slack_tasks.handle_interactive`へ振り分け。
- `slack_bot.apply_to_db` — `@NegoCollection`確定時、割当済み・未消化のJamie全文（`sfa_db.find_assigned_jamie_transcript`）があれば本文に採用しSlack入力を強調として追記、消化済み(`status='saved'`)にマーク。
- `render.yaml`の`sfa-crm`サービスに`SALES_CHANNEL_ID`を追加（従来cronのみに設定されていた）。

**未実装（次回以降のフォローアップ）**:
- [ ] Web格上げ（ヒアリングテンプレ起票／重いステージ変更時に「[SFAで仕上げる]」を提示する導線）は今回未実装。現状はSlack確定 or 従来のWeb `/intake-inbox` 経由手動遷移のいずれかで代替可能。
- [ ] Jamie全文の見せ方（候補提示メッセージ自体には全文を出していない。要約表示や全文プレビューが要るかは運用してから判断）。
