# koenidashiteko

X (旧Twitter) アカウント **声なき多数派 (@koenidashiteko)** 用の自動投稿システム。

令和の職場・社会の風潮（タイパ、過度なコンプラ、形だけの働き方改革、1on1、
ハラスメント過敏、手当なき新人教育など）に振り回される社会人のリアルな本音を
ユーモアを交えて代弁する投稿を、Gemini API で自動生成し、承認フローを経て
X に画像付きで自動投稿する。

## 仕組み

1. **下書き生成** (`generate_draft.py`)
   - Gemini（テキストモデル）でペルソナに沿った本音ぼやきテキストと、
     それを象徴する風刺画イラストの生成プロンプトを作成。
   - Imagen (`imagen-3.0-generate-002`) で風刺画像を生成し `images/queue/` に保存。
   - 画像をコミット＆プッシュしたうえで、内容をプレビューする GitHub Issue を作成
     （`draft` ラベル）。
2. **人間によるレビュー**
   - Issue の内容（テキスト＋画像）を確認し、問題なければ Issue に
     `approved` ラベルを付ける。
3. **承認後投稿** (`post_approved.py`)
   - `approved` ラベル付与時、および15分おきの定時チェックで起動。
   - 画像を X API (v1.1 media upload) でアップロードし、テキストと合わせて
     画像付きポストとして投稿。
   - 投稿後、画像を `images/queue/` から `images/posted/` に移動してコミット＆
     プッシュし、Issue にツイートURLを記載してクローズ（`posted` ラベル）。
   - 失敗時は Issue にエラー内容をコメントし `post-failed` ラベルを付与
     （Issue は open のまま）。

## 定時実行スケジュール

- `generate_draft.yml`: 毎日 06:30 JST / 17:30 JST に下書きを生成。
- `post_approved.yml`: `approved` ラベル付与時に即時実行、加えて15分おきに
  未処理の `approved` Issue をチェック。

## セットアップ

### 1. リポジトリシークレット

`Settings > Secrets and variables > Actions` に以下を登録する。

| シークレット名 | 用途 |
| --- | --- |
| `GEMINI_API_KEY` | Google Gemini API キー（テキスト・画像生成） |
| `X_API_KEY` | X API Consumer Key |
| `X_API_SECRET` | X API Consumer Secret |
| `X_ACCESS_TOKEN` | X API Access Token（投稿権限付き） |
| `X_ACCESS_TOKEN_SECRET` | X API Access Token Secret |

`GITHUB_TOKEN` はワークフロー実行時に GitHub Actions が自動発行するため
登録不要。

### 2. ワークフロー権限

`Settings > Actions > General > Workflow permissions` を
**Read and write permissions** に設定する（Issue 作成・画像コミットに必要）。

### 3. ローカルでのテスト

```bash
pip install -r requirements.txt

# 下書き生成をテスト（GEMINI_API_KEY, GITHUB_TOKEN, GITHUB_REPOSITORY が必要）
export GEMINI_API_KEY=xxxx
export GITHUB_TOKEN=xxxx
export GITHUB_REPOSITORY=serenorasalon/koenidashiteko
python generate_draft.py

# 承認後投稿をテスト（X の各種キーが必要）
export X_API_KEY=xxxx
export X_API_SECRET=xxxx
export X_ACCESS_TOKEN=xxxx
export X_ACCESS_TOKEN_SECRET=xxxx
export ISSUE_NUMBER=1
python post_approved.py
```

## ディレクトリ構成

```
.
├── generate_draft.py          # 下書き＆風刺画自動生成
├── post_approved.py           # 承認後投稿
├── requirements.txt
├── images/
│   ├── queue/                 # 承認待ちの画像
│   └── posted/                # 投稿済み画像
└── .github/workflows/
    ├── generate_draft.yml
    └── post_approved.yml
```
