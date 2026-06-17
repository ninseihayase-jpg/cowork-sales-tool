# CRM → SFA 吸収計画・引き継ぎ書

> **目的**: 別フォルダで並行開発された「CRMツール（cowork_CRM）」を本SFAプロジェクトに統合する。
> 本書は機能重複の分析・吸収の設計・実装手順・廃止計画を集約する。
> 作成: 2026-06-18 / Claude Code（主の指示に基づく）

---

## 0. 背景と経緯

2026-06-18、主の指示でCRMツールを並行開発した（`営業支援ツール(CRM)/cowork_CRM/crm-app/`）。
調査の結果、SFAと機能が重複することが判明。主の判断で **案B（SFAに統合）** を採用。
本書はその引き継ぎ文書。

---

## 1. CRMツールで開発した内容（引き継ぎ資産）

### 1.1 技術スタック
| 要素 | 内容 |
|------|------|
| フロントエンド | React 19 + TypeScript + Vite 8 + Tailwind CSS v4 |
| バックエンド/DB | Supabase（PostgreSQL + リアルタイム sync）|
| デモモード | Supabase未設定時 → localStorage で完全動作 |
| ホスティング想定 | Vercel（GitHub連携で自動デプロイ）|
| 場所 | `営業支援ツール(CRM)/cowork_CRM/crm-app/` |

### 1.2 実装済み機能一覧
| 機能 | 実装状況 | 概要 |
|------|---------|------|
| コンタクト一覧 | ✅ | 検索・ステータス/テーマ/獲得経路フィルタ・ソート |
| コンタクト詳細 | ✅ | 全フィールド表示・ステータス更新ボタン・Gmailリンク |
| コンタクト追加/編集 | ✅ | モーダルフォーム（スマホ対応、必須項目最小化）|
| コンタクト削除 | ✅ | 確認ダイアログ付き |
| CSV一括取込 | ✅ | 展示会後の名刺データ一括インポート（ヘッダ自動マップ）|
| 活動ログ（コンタクト紐付） | ✅ | メモ/メール/電話/面談を時系列記録 |
| ピッチテーマ管理 | ✅ | テーマの作成・色設定・アーカイブ・成約率追跡 |
| ダッシュボード | ✅ | 総件数・今月新規・テーマ別パフォーマンス・ファネル |
| レスポンシブUI | ✅ | スマホ（展示会でのリアルタイム入力）対応 |

### 1.3 データモデル（CRM独自部分）

```sql
-- Supabase (PostgreSQL)
contacts:
  id uuid, name text, company text, title text,
  email text, phone text,
  source: 'exhibition'|'referral'|'inbound'|'other',   ← SFAにない
  theme_id uuid → themes.id,                           ← SFAにない（ピッチテーマ）
  status: 'new'|'following'|'meeting'|'proposal'|'won'|'lost',
  notes text, assigned_to text,
  created_at, updated_at

themes:                                                 ← SFAに完全にない概念
  id uuid, name text, description text,
  color text,        ← 8色からビジュアル識別
  is_active boolean, created_at

activity_logs:                                          ← SFAはdeals紐付け、CRMはcontacts紐付け
  id uuid, contact_id uuid → contacts.id,
  type: 'note'|'email'|'call'|'meeting',
  content text, author text, created_at
```

### 1.4 CRMの設計思想（SFA設計との違い）

**CRMが解こうとした課題**
- 展示会・知人紹介で大量獲得した「名刺の人」を追跡する
- ピッチテーマをピボットしながら、どのネタが刺さるか測定する
- チーム全員（複数名の社員）が共有で使う

**SFAとの本質的な粒度差**
| 軸 | CRM | SFA |
|----|-----|-----|
| 管理単位 | コンタクト（人）| アカウント（企業）+ 商談 |
| ファネル段階 | 早期（名刺→初回商談）| 中後期（商談→受注）|
| テーマの意味 | ピッチアプローチのカテゴリ（複数人に同テーマ）| 個別案件（1社1テーマDB行）|
| 件数規模 | 数百〜数千件 | 数十件 |

---

## 2. 機能重複マップ

| CRM機能 | SFAの現状 | 判定 | 対応方針 |
|---------|----------|------|---------|
| コンタクト管理（人単位）| contacts テーブルあり（薄い・account紐付け前提）| **部分重複** | SFA contacts を拡張してCRMコンタクト概念を吸収 |
| ピッチテーマ管理 | なし | **SFAに未存在** | `pitch_themes` テーブルを新設 |
| 獲得経路（source）| deal.lead_pattern で対応（Connection/Exh.等）| **表現差あり** | leads に `source` カラム追加。lead_patternと統合 |
| 活動ログ（人紐付）| activities（deal紐付）のみ | **紐付け先が違う** | leads紐付けの活動ログを追加 |
| ステータスパイプライン | deals.stage（リード〜受注）| **レイヤー違い** | leads に `lead_status` を追加（early funnel用）|
| CSV一括取込 | なし | **SFAに未存在** | webapp.py に `/leads/import` エンドポイント追加 |
| ダッシュボード | `/dashboard`（秘書側、高機能）| **秘書ダッシュボードで充足** | CRMダッシュボードは不要 |
| レスポンシブUI | webapp.py（基本CSS）| **CRMが優れる** | UIの知見をwebapp.py CSS改善に活用 |
| チーム共有 | ローカル動作（Render未デプロイ）| **SFAのRender展開で解決** | Supabase不要。Render上のSQLiteで対応 |

---

## 3. SFAへの吸収設計

### 3.1 設計方針

**「SFAに早期ファネル管理モジュール（Leads）を追加する」**

SFAが既に持つ accounts/contacts/deals/activities の構造を活かし、
CRMの「大量コンタクト→ピッチテスト→商談化」を担う **Leads モジュール** として統合する。

```
[早期ファネル: Leads モジュール（新設）]
  leads（名刺の人）× pitch_themes（ピッチアプローチ）× lead_activities
         ↓ 「商談化」ボタン（meeting以降で使用）
[中後期ファネル: 既存 SFA]
  accounts × contacts × deals × activities
         ↓ 同期
[可視化: 既存]
  テーマDB → Salesダッシュボード（秘書）
```

### 3.2 追加するDBスキーマ（sfa_db.py への追記）

```sql
-- ① ピッチテーマ（CRMから移植）
CREATE TABLE IF NOT EXISTS pitch_themes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,          -- 例: "AI業務自動化"
    description TEXT,
    color       TEXT DEFAULT '#6366f1', -- 8色から選択
    is_active   INTEGER DEFAULT 1,      -- 0=アーカイブ
    created_at  TEXT DEFAULT (datetime('now'))
);

-- ② リード（CRMのcontacts相当。account紐付け前の「人」）
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    company         TEXT NOT NULL,
    title           TEXT,
    email           TEXT,
    phone           TEXT,
    source          TEXT DEFAULT 'other',  -- exhibition/referral/inbound/other
    pitch_theme_id  INTEGER REFERENCES pitch_themes(id) ON DELETE SET NULL,
    lead_status     TEXT DEFAULT 'new',    -- new/following/meeting/proposal/won/lost
    notes           TEXT,
    assigned_to     TEXT,
    deal_id         INTEGER REFERENCES deals(id) ON DELETE SET NULL, -- 商談化後に設定
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(lead_status);
CREATE INDEX IF NOT EXISTS idx_leads_theme  ON leads(pitch_theme_id);

-- ③ リード活動ログ（CRMのactivity_logs相当）
CREATE TABLE IF NOT EXISTS lead_activities (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    type       TEXT DEFAULT 'note',  -- note/email/call/meeting
    content    TEXT NOT NULL,
    author     TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lead_activities_lead ON lead_activities(lead_id);
```

### 3.3 ステータス・獲得経路の統合整理

**リードステータス（lead_status）← CRM のステータスをそのまま採用**

| 値 | 日本語 | 商談化の目安 |
|----|--------|-------------|
| new | 新規 | - |
| following | フォロー中 | - |
| meeting | 商談中 | ← ここから「商談化」ボタン活性化 |
| proposal | 提案済 | ← 強く推奨 |
| won | 成約 | - |
| lost | 失注 | - |

**獲得経路（source）← CRMのsourceをベースに、SFAのlead_patternと対応**

| leads.source（新規）| SFA deal.lead_pattern（既存）| 意味 |
|---------------------|------------------------------|------|
| exhibition | Exh. | 展示会 |
| referral | Connection / Advisor | 知人・紹介 |
| inbound | HP / SNS | インバウンド |
| other | Partner / PE / Under / na | その他 |

※ `deals.lead_pattern` は既存テーマDB連携のため変更しない（後方互換）

### 3.4 webapp.py への追加ルート

```
GET  /leads               → リード一覧（検索・status/theme/source フィルタ）
GET  /leads/new           → リード追加フォーム
POST /leads/save          → リード保存（新規/更新）
GET  /leads/{id}          → リード詳細 + 活動ログ
POST /leads/{id}/activity → 活動追加
POST /leads/{id}/status   → ステータス更新（クイック）
POST /leads/{id}/convert  → 商談化（leads → accounts + deals）
POST /leads/import        → CSV一括取込

GET  /pitch_themes        → テーマ一覧 + リード数・成約率集計
POST /pitch_themes/save   → テーマ追加・編集
POST /pitch_themes/{id}/toggle → アーカイブ/復元
```

### 3.5 「商談化」ワークフロー（leads → accounts + deals）

```
lead (leads table)  [lead_status >= 'meeting']
  ↓ POST /leads/{id}/convert
  1. accounts に企業を INSERT（または既存を名前検索）
  2. contacts に担当者を INSERT
  3. deals に商談を INSERT（lead_status → deal.stage にマップ）
  4. leads.deal_id = 作成したdeals.id にセット（紐付け完了）
  5. → /deal/{deal_id} にリダイレクト（以後はSFA通常フロー）
```

**ステータスマッピング（lead_status → deal.stage）**

| lead_status | deal.stage |
|-------------|-----------|
| new | リード |
| following | アポ獲得 |
| meeting | 初回アポ実施 |
| proposal | 提案 |
| won | 受注 |
| lost | 失注 |

---

## 4. CSV取込仕様（CRMから移植）

### 対応CSVフォーマット（ヘッダ名）

```csv
名前,会社名,役職,メール,電話,獲得経路,ピッチテーマ,ステータス,メモ,担当者
田中 太郎,株式会社○○,営業部長,tanaka@example.com,090-...,exhibition,AI業務自動化,new,展示会で名刺交換,
```

| CSVヘッダ | DBカラム | 備考 |
|----------|---------|------|
| 名前 / name | leads.name | 必須（空行はスキップ）|
| 会社名 / company | leads.company | 必須 |
| 役職 / title | leads.title | |
| メール / email | leads.email | |
| 電話 / phone | leads.phone | |
| 獲得経路 / source | leads.source | 不明値 → 'other' |
| ピッチテーマ / pitch_theme | pitch_themes.name で検索 → pitch_theme_id | 不一致 → NULL |
| ステータス / status | leads.lead_status | 不明値 → 'new' |
| メモ / notes | leads.notes | |
| 担当者 / assigned_to | leads.assigned_to | |

実装先: `cowork/leads_csv.py`（新規ファイル）

---

## 5. 実装ロードマップ

| Step | 作業 | ファイル | 状態 |
|------|------|---------|------|
| 1 | DBスキーマ追加（pitch_themes / leads / lead_activities）| `cowork/sfa_db.py` | ✅ 2026-06-18 |
| 2 | CRUD関数追加（list_leads / get_lead / upsert_lead / list_lead_activities / create_lead_activity）| `cowork/sfa_db.py` | ✅ 2026-06-18 |
| 3 | CSV取込ロジック実装 | `cowork/leads_csv.py`（新規）| ✅ 2026-06-18 |
| 4 | webapp.py に /leads/* ルート追加 | `cowork/webapp.py` | ✅ 2026-06-18 |
| 5 | webapp.py に /pitch_themes/* ルート追加 | `cowork/webapp.py` | ✅ 2026-06-18 |
| 6 | 「商談化」ワークフロー実装（/leads/{id}/convert）| `cowork/webapp.py` | ✅ 2026-06-18 |
| 7 | UIのモバイル対応改善（CSS）| `cowork/webapp.py` | ✅ 2026-06-18 |
| 8 | Renderデプロイ設定（render.yaml）| ルート直下 | ✅ 2026-06-18 |
| 9 | CRMフォルダの廃止 | `営業支援ツール(CRM)/` | ⬜ Step1-8完了。随時可 |

---

## 6. CRMプロジェクトの廃止計画

### 廃止対象
`営業支援ツール(CRM)/cowork_CRM/crm-app/` 全体

### 廃止時の資産移行チェックリスト

| CRM資産 | SFAへの移行先 | 移行ステップ |
|---------|-------------|------------|
| CSV解析ロジック（`storage.ts:parseContactsCsv`）| `cowork/leads_csv.py` | Step 3 |
| ステータス定義・色定義（`types.ts`）| webapp.py の CSS・定数 | Step 7 |
| 活動ログUI（`ContactDetail.tsx`）| webapp.py の活動ログHTML | Step 4 |
| DBスキーマ（`supabase_schema.sql`）| `sfa_db.py` の SCHEMA 定数 | Step 1 |
| CRMリサーチ知見 | `docs/00_設計構想.md §1.2` に反映済み | 完了 |

### 廃止しないもの（SFAで不要）
- Supabase 依存（SFAはSQLite + Render）
- React/Vite/TypeScript（SFAはPython + 標準ライブラリ）
- Vercel デプロイ設定
- デモモード（localStorageフォールバック）

---

## 7. 変更しない既存機能（安定資産）

以下は CRM吸収の影響を受けない：

- `cowork/sync.py` + `mapping.py`：フェーズ1のスプシ→テーマDB同期
- `cowork/theme_link.py`：deals → テーマDB 同期
- `cowork/theme_db.py`：/api/execute クライアント
- `cowork/sources.py`：Google Sheets / xlsx 読み取り
- 既存の accounts / contacts / deals / activities テーブル（後方互換保証）
- `.github/` ワークフロー
- 秘書 `/dashboard`（Salesダッシュボードはそのまま使う）

---

## 8. なぜ Supabase ではなく SQLite+Render か

CRMはチーム共有のためにSupabaseを選択した。SFAがSQLite+Renderを選ぶ理由：

1. **既存方針との一貫性**：秘書（hisho）もRender+SQLite。SFAも同方式が自然
2. **テーマDB連携がRender内で完結**：`/api/execute` 経由の疎結合をそのまま活用できる
3. **運用サービス数を増やさない**：Supabaseを追加すると管理対象が増える
4. **認証**：チーム共有はRenderのBasic Auth or 社内IPホワイトリストで対応可

---

## 9. 開発ログ（CRM開発の記録）

| 日時 | 作業 | 成果物 |
|------|------|--------|
| 2026-06-18 | CRMリサーチ（deep-research、109 agents）| CRM要件・落とし穴の明確化 |
| 2026-06-18 | CRM v1実装（React+Supabase）| `crm-app/` 一式 |
| 2026-06-18 | SFA・秘書との重複分析 | 重複マップ（§2）|
| 2026-06-18 | 吸収計画策定 | 本書 |

**CRMリサーチで得た知見（`docs/00_設計構想.md §1.2` に反映済み）**
- CRM最大の失敗要因 = 入力負荷（業界の定着率は平均26%という調査も存在）
- 中小企業の典型的失敗 = 情報が個人の記憶/Excelに残り、退職時に消える
- AI自動入力（2025〜）により入力負荷問題は軽減されつつある → Phase 2-2 Slack連携は正しい方向

---

*本書更新ルール：各Stepが完了したら §5 の状態を ✅ に更新する。廃止計画が完了したら §6 を閉じる。*

