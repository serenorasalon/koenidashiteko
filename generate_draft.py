"""
声なき多数派 (@koenidashiteko) 下書き＆風刺画自動生成スクリプト。

Gemini API でペルソナに基づく「本音ぼやきテキスト」と、それを象徴する
風刺画イラストの生成プロンプトを作成し、Pollinations.ai (FLUX/Turbo) で
画像を生成する。生成した画像は images/queue/ に保存してリポジトリに
コミット＆プッシュし、テキストと画像プレビューを載せた GitHub Issue を作成する。
"""

import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
import uuid
from datetime import datetime, timezone, timedelta

import requests
from google import genai
from google.genai import types

# API が動的なモデル一覧取得に失敗した場合にのみ使う最終フォールバック。
# （通常は client.models.list() で取得した現行モデルが優先される）
STATIC_TEXT_MODEL_FALLBACKS = [
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-2.0-flash-exp",
]
# テキスト生成モデルの一覧から除外する（generateContent はサポートするが
# 用途が異なる）モデル名の断片。
TEXT_MODEL_EXCLUDE = ("imagen", "embedding", "aqa", "-image")

POLLINATIONS_BASE_URL = "https://image.pollinations.ai/prompt"
IMAGE_WIDTH = 1200
IMAGE_HEIGHT = 1200
# flux-anime が非対応/失敗した場合は flux にフォールバックする。
IMAGE_MODEL_CANDIDATES = ["flux-anime", "flux"]
IMAGE_RETRIES_PER_MODEL = 2
# Gemini が考案したシチュエーション（image_prompt）を、この固定スタイルで必ず
# 描かせる（Gemini の出力内容に依存しない強制指定）。
ILLUSTRATION_STYLE = (
    "Japanese monochrome 2d doodle webcomic by UNOKINOKI style, loose black "
    "ink pen line art on clean pure white background, cute simple human boy "
    "in black hoodie with funny deadpan face, flat 2D cartoon, minimal "
    "accent color, hand-drawn comic strip, sticker style, strictly NO 3d "
    "render, strictly NO plastic texture, strictly NO text, strictly NO "
    "speech bubbles"
)
# Pollinations の画像エンドポイントには独立したネガティブプロンプト欄がない
# ため、"no " を前置してポジティブなプロンプト文字列の中で機能させる。
IMAGE_NEGATIVE_KEYWORDS = (
    "photorealistic, 3d render, detailed background, realistic face, fine "
    "art, complex shadows, gloom, dark background, blurry, typography, logo"
)


def _negative_clause() -> str:
    items = [item.strip() for item in IMAGE_NEGATIVE_KEYWORDS.split(",")]
    return ", ".join(f"no {item}" for item in items)

JST = timezone(timedelta(hours=9))

PERSONA_PROMPT = """あなたはX（旧Twitter）の匿名アカウント「声なき多数派」(@koenidashiteko) の
中の人です。新人社員・アルバイト・20代若手が思わず「それな」「わかりすぎる」と
共感してクスッと笑える、身近でポップな日常あるあるを代弁する投稿を1つ
作成してください。難しい時事問題や組織論は扱わないこと。

# 題材（この中から柔軟に、ランダムに1つ選ぶこと。特定のテーマに偏らないこと）
- バイト・新人あるある: 何でも聞いてねと言われて聞きに行ったら「今忙しいから
  後にして」と言われる絶望、メモを取るスピードが追いつかない、電話を取る時の
  異様な緊張感
- 仕事・シフトあるある: 出勤前の布団の引力が普段の5倍になる、バイト先の
  まかないや休憩時間だけを楽しみに生きている、「明日休み？」と聞かれた瞬間の
  シフト代行警戒アラート
- 日常・お金・スマホ: 給料日の3日後には残高が初期化されている、スマホの
  充電10%で始まる退勤時のサバイバル、コンビニで新作スイーツを買うだけで
  予算オーバー
- 気象・通勤: 大雨の日に限って靴下に穴が開いている、月曜朝の目覚まし
  アラーム音に対する強い殺意

# 投稿のルール（文字数を最優先すること）
- 25〜50文字程度の短くポップな一言にすること。
- フォロワーへの問いかけ・アンケート・「〜ですよね？」のような質問形式は禁止。
  あくまで独り言・ぼやき・本音の吐露として書くこと。
- 説教くささ・小難しさはゼロにすること。ユーモア全開の「心の叫び・愛嬌のある
  ぼやき」にすること。ネガティブすぎず、「それな」「わかりすぎる」と思わず
  クスッと笑えるようにすること。
- 絵文字やハッシュタグは使わないこと。

# 参考例
「『何かあったら聞いてね』の言葉を信じて聞きに行ったら『今忙しい』は詐欺。」
「出勤前の布団、明らかに普段の5倍の引力で私を離してくれない。」
「給料日の3日後に残高を見る勇気、誰か私にください。」

# 出力形式
以下のキーのみを持つ JSON オブジェクトを1つだけ出力してください。
説明文やマークダウンのコードフェンスは付けないこと。

{
  "text": "生成した投稿本文（25〜50文字程度、日本語）",
  "image_prompt": "この投稿を象徴するコミカルなイラストを生成するための英語の画像生成プロンプト"
}

# image_prompt の作り方（誰が見ても直感的に意味がわかることが最優先）

image_prompt は、椅子に座っているだけの人物のような抽象的で意味不明な絵に
絶対にならないよう、必ず次の3ステップで、この順番のとおりに考案してください。

## ステップ1: キーワードの特定
生成した text の中から、そのぼやきの「メインの題材」を1つ特定してください
（例: 電話対応、布団/出勤、残高/財布、シフト、スマホの充電、月曜の目覚まし
アラーム など）。

## ステップ2: 「白背景＋脱力系の人間キャラ」の極めてシンプルな構図
「重圧」「責任回避」「孤独」「暗闇」「プレッシャー」のような感情・抽象概念の
単語を image_prompt に一切書いてはいけません。日用品を擬人化する（道具自体に
顔や手足を生やして動かす）ことも完全に禁止です。必ず次の要素だけで構成される、
極めてシンプルな構図にしてください（これ以外の複雑な構図は禁止）:

  - 背景: 何もない真っ白な背景（pure white background）のみ。
    オフィスや部屋などの背景描写は一切禁止。
  - 主体: パーカーやスーツを着た、シンプルで脱力感のある「人間」の
    キャラクターを1〜2体（道具や動物を主役にしないこと）。
  - 状況: その人間キャラクターが、身近な日用品（電話・布団・財布・
    スマホ等）と一緒にシュールでコミカルなポーズ・状況になっている様子。

小学生が見ても一目で「何が起きているか」分かって思わず笑えるくらい具体的で
分かりやすいこと。複雑な背景・小道具の羅列・写実的な顔は完全に禁止です。

（例1）text:「スマホから離れられない」
  ✓ 良い例: A simple 2D doodle drawing of a young person in a black
    hoodie lying on the floor, staring blankly at a smartphone, loose
    black ink lines on pure white background.
（例2）text:「布団から出られない」
  ✓ 良い例: A cute 2D hand-drawn doodle person completely wrapped in a
    futon blanket like a burrito with a deadpan face, thick black pen
    lines on white background.
（例3）text:「給料日の3日後に残高がゼロになる」
  ✓ 良い例: A funny simple 2D doodle person opening an empty flat wallet
    with a blank expression, minimal line art on pure white background.

## ステップ3: 英語プロンプトの構成
[ステップ2で組み立てた白背景＋キャラクター＋日用品の構図] を、そのまま1つの
英文としてまとめてください。必ず "pure white background" を含め、感情・
抽象概念を表す単語（pressure, despair, isolation, darkness 等）は一切
含めないこと。スタイル指定・ネガティブ指定はコード側で自動的に末尾に直結
されるため、image_prompt にはスタイルキーワードを含めなくてよい。曖昧な
形容詞だけで済ませず、誰が読んでも同じ絵を思い浮かべられる具体性が必須です。
"""


def build_gemini_client() -> genai.Client:
    api_key = os.environ["GEMINI_API_KEY"]
    return genai.Client(api_key=api_key)


def _short_model_name(full_name: str) -> str:
    return (full_name or "").rsplit("/", 1)[-1]


def _version_sort_key(name: str) -> tuple:
    """モデル名に含まれる数値から新しい順にソートするためのキー。"""
    return tuple(float(n) for n in re.findall(r"\d+(?:\.\d+)?", name))


def discover_models(
    client: genai.Client, action: str | None = None, name_contains: str | None = None
) -> list[str]:
    """API キー/アカウントで実際に利用可能なモデル名を新しい順に取得する。

    `client.models.list()` の呼び出し自体に失敗した場合（権限不足・
    ネットワークエラー等）は空リストを返し、呼び出し元は静的フォールバック
    リストのみで動作を継続する。
    """
    names: list[str] = []
    try:
        for model in client.models.list():
            actions = model.supported_actions or []
            if action and action not in actions:
                continue
            short = _short_model_name(model.name)
            if not short:
                continue
            if name_contains and name_contains not in short:
                continue
            names.append(short)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[models] モデル一覧の動的取得に失敗しました。静的フォールバックのみ"
            f"使用します: {exc}",
            file=sys.stderr,
        )
        return []
    names.sort(key=_version_sort_key, reverse=True)
    return names


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def build_text_model_candidates(client: genai.Client) -> list[str]:
    discovered = discover_models(client, action="generateContent")
    discovered = [
        name
        for name in discovered
        if not any(excluded in name for excluded in TEXT_MODEL_EXCLUDE)
    ]
    return _dedupe(discovered + STATIC_TEXT_MODEL_FALLBACKS)


def generate_text_and_prompt(client: genai.Client) -> dict:
    candidates = build_text_model_candidates(client)
    print(f"[text] モデル候補（優先順）: {candidates}")
    last_error: Exception | None = None

    for model in candidates:
        try:
            response = client.models.generate_content(
                model=model,
                contents=PERSONA_PROMPT,
                config=types.GenerateContentConfig(
                    temperature=1.0,
                    response_mime_type="application/json",
                ),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[text] モデル '{model}' の呼び出しに失敗しました: {exc}", file=sys.stderr)
            last_error = exc
            continue

        raw = response.text.strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)

        text = data["text"].strip()
        image_prompt = data["image_prompt"].strip()
        if not text or not image_prompt:
            raise ValueError(f"Gemini から空の値が返されました: {data!r}")

        print(f"[text] モデル '{model}' でテキストを生成しました。")
        return {"text": text, "image_prompt": image_prompt}

    raise RuntimeError(
        f"すべてのテキスト生成モデル候補 {candidates} で失敗しました"
    ) from last_error


def generate_image(image_prompt: str) -> bytes:
    """Pollinations.ai で風刺画像を生成する。APIキー不要。

    Gemini が考案したビジュアルメタファーに、固定の John Holcroft 風
    エディトリアル・スタイル指定を必ず付与してから送信する。
    `flux-anime` が使えない場合は `flux` にフォールバックする。
    画像は必須のため、すべてのモデル・リトライが尽きた場合は例外を送出して
    ワークフローを失敗させる（画像なしの Issue は作成しない）。
    """
    full_prompt = f"{image_prompt}, {ILLUSTRATION_STYLE}, {_negative_clause()}"
    encoded_prompt = urllib.parse.quote(full_prompt, safe="")

    last_error: Exception | None = None
    for model in IMAGE_MODEL_CANDIDATES:
        for attempt in range(1, IMAGE_RETRIES_PER_MODEL + 1):
            seed = random.randint(0, 2**31 - 1)
            url = (
                f"{POLLINATIONS_BASE_URL}/{encoded_prompt}"
                f"?width={IMAGE_WIDTH}&height={IMAGE_HEIGHT}&model={model}"
                f"&nologo=true&seed={seed}"
            )
            print(
                f"[image] Pollinations.ai へリクエストします "
                f"(model={model}, 試行 {attempt}/{IMAGE_RETRIES_PER_MODEL}, seed={seed})"
            )
            try:
                response = requests.get(url, timeout=60)
                response.raise_for_status()

                content_type = response.headers.get("Content-Type", "")
                if not content_type.startswith("image/"):
                    raise RuntimeError(
                        f"Pollinations.ai が画像以外のレスポンスを返しました "
                        f"(Content-Type: {content_type!r})"
                    )
                if not response.content:
                    raise RuntimeError("Pollinations.ai が空のレスポンスを返しました")
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[image] model={model} 試行 {attempt}/{IMAGE_RETRIES_PER_MODEL} "
                    f"が失敗しました: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                last_error = exc
                if attempt < IMAGE_RETRIES_PER_MODEL:
                    wait_seconds = 2**attempt
                    print(f"[image] {wait_seconds}秒待機してリトライします。")
                    time.sleep(wait_seconds)
                continue

            print(
                f"[image] 画像を取得しました "
                f"（model={model}, {len(response.content)} bytes）"
            )
            return response.content

        print(f"[image] model={model} をあきらめ、次の候補にフォールバックします。", file=sys.stderr)

    raise RuntimeError(
        f"Pollinations.ai への画像生成リクエストがすべてのモデル候補 "
        f"{IMAGE_MODEL_CANDIDATES} で失敗しました"
    ) from last_error


def save_image(image_bytes: bytes) -> str:
    now = datetime.now(JST)
    filename = f"{now.strftime('%Y%m%d_%H%M')}_{uuid.uuid4().hex[:8]}.png"
    rel_path = os.path.join("images", "queue", filename)
    with open(rel_path, "wb") as f:
        f.write(image_bytes)
    return rel_path.replace("\\", "/")


def git_commit_and_push(paths: list[str], message: str) -> None:
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        check=True,
    )
    subprocess.run(["git", "add", *paths], check=True)
    status = subprocess.run(
        ["git", "diff", "--staged", "--quiet"],
    )
    if status.returncode == 0:
        print("コミット対象の変更がありません。スキップします。")
        return
    subprocess.run(["git", "commit", "-m", message], check=True)
    subprocess.run(["git", "push"], check=True)


def create_issue(text: str, image_path: str) -> dict:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    branch = os.environ.get("GITHUB_REF_NAME", "main")

    raw_image_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{image_path}"
    meta = json.dumps({"text": text, "image_path": image_path}, ensure_ascii=False)

    body = (
        f"## 下書きテキスト\n\n"
        f"> {text}\n\n"
        f"## 風刺画プレビュー\n\n"
        f"![draft image]({raw_image_url})\n\n"
        f"---\n"
        f"このIssueに `approved` ラベルを付けると自動投稿されます。\n\n"
        f"<!--KOENIDASHITEKO_META\n{meta}\n-->\n"
    )

    now_jst = datetime.now(JST)
    title = f"[下書き] {now_jst.strftime('%Y-%m-%d %H:%M')} JST - {text[:20]}"

    resp = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        json={"title": title, "body": body, "labels": ["draft"]},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    client = build_gemini_client()

    draft = generate_text_and_prompt(client)
    print(f"生成テキスト: {draft['text']}")
    print(f"画像プロンプト: {draft['image_prompt']}")

    image_bytes = generate_image(draft["image_prompt"])
    image_path = save_image(image_bytes)
    print(f"画像を保存しました: {image_path}")
    git_commit_and_push([image_path], f"chore: add draft image {os.path.basename(image_path)}")

    issue = create_issue(draft["text"], image_path)
    print(f"Issue を作成しました: {issue['html_url']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"エラーが発生しました: {exc}", file=sys.stderr)
        sys.exit(1)
