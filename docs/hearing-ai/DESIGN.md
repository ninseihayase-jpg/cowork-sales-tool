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
| 4 | データ/確定フロー | hearing_sessions 追加、確定→hearing_results＋活動履歴＋NextStep化 | 未 |
| 5 | Claude整形 | 既存 `_call_claude_haiku` 拡張。項目別/全体像/NextStep/メール素案 | 未 |
| 6 | 確認UI | 取り込み→整形結果を人が編集・確定する画面（既存hearing UIを拡張） | 未 |
| 7 | メール素案→ドラフト | 生成メールをOutlookドラフト等へ（#65と連携余地） | 未 |
| 8 | 秘匿/コンプラ | APIキーはRender秘匿、録音同意運用、EUデータresidency前提、公開リポジトリ配慮 | 継続 |

## 7. フェーズ計画（各フェーズで人の確認→確定）
- **P0**: 本設計書作成（本コミット）。方針・調査結果を記録。 ← 今ここ
- **P1**: Jamie連携PoC（ゲート1b→2→3）。webhook受信＋REST取得で「1面談の文字起こし/サマリ」をSFAに取り込めることを実証。
- **P2**: Claude整形（ゲート5）。取り込んだ文字起こし→ヒアリング項目別/全体像/NextStep/メール素案。
- **P3**: 確認・確定UI（ゲート4,6）。人が編集→hearing_results/活動履歴/NextStep確定。
- **P4**: メール素案→ドラフト（ゲート7）。
- **P5**: リアルタイム化の再検討（案R）。別STT/会議ボットAPIの技術検証→可能なら StreamingSTTSource を追加。

## 8. 未確定・次アクション
- [ ] **ユーザー**: Jamie の Settings→Developers→API Keys でキー作成可否＆プランを確認（ゲート1b）。
- [ ] キー確保後、Render(sfa-crm)の秘匿設定に `JAMIE_API_KEY` を格納（値は非コミット）。
- [ ] Webhook受信URL（`/api/jamie/webhook`）と署名検証仕様をDocsで確認しP1着手。
