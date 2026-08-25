"""
声なき多数派 (@koenidashiteko) 下書き＆風刺画自動生成スクリプト。

Gemini API でペルソナに基づく「本音ぼやきテキスト」と、それを象徴する
風刺画イラストの生成プロンプトを作成し、Pollinations.ai (FLUX/Turbo) で
画像を生成する。生成した画像は images/queue/ に保存してリポジトリに
コミット＆プッシュし、テキストと画像プレビューを載せた GitHub Issue を作成する。
"""

import io
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
from PIL import Image, ImageDraw, ImageFont

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
IMAGE_MODEL = "flux"
IMAGE_MAX_RETRIES = 3
ILLUSTRATION_STYLE = (
    "Japanese single-panel satirical office webcomic, high contrast flat vector art, "
    "bold clean outlines, muted tones, surreal corporate life metaphor, silent comic "
    "style, no speech bubbles, strictly NO text, strictly NO words, strictly NO "
    "typography, strictly NO dialogue"
)

# apt-get install fonts-noto-cjk（Ubuntu/GitHub Actions）で入る標準パスを優先し、
# 環境によって異なる ipaexfont のパッケージ配置や、ローカル Windows での動作確認用に
# 使えるフォントもフォールバックとして並べている。
CAPTION_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/ipaexfont-gothic/ipaexg.ttf",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/truetype/ipa-gothic/ipag.ttf",
    "C:/Windows/Fonts/YuGothB.ttc",
    "C:/Windows/Fonts/meiryob.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
]
CAPTION_MAX_CHARS = 40
CAPTION_MAX_CHARS_PER_LINE = 14
CAPTION_MAX_LINES = 3

JST = timezone(timedelta(hours=9))

PERSONA_PROMPT = """あなたはX（旧Twitter）の匿名アカウント「声なき多数派」(@koenidashiteko) の
中の人です。令和の職場や社会の風潮――タイパ重視、過度なコンプライアンス、
形だけの働き方改革、意味のない1on1、ハラスメントへの過敏な反応、手当の出ない
新人教育の押し付けなど――に日々振り回されて消耗している社会人の本音を、
ユーモアを交えて代弁する投稿を1つ作成してください。

# 投稿のルール
- 40〜100文字程度の短くキレのある一言二言にすること。
- フォロワーへの問いかけ・アンケート・「〜ですよね？」のような質問形式は禁止。
  あくまで独り言・ぼやき・本音の吐露として書くこと。
- ネガティブすぎず、「それな」「わかりすぎる」と思わずクスッと笑えるような
  ブラックユーモアを効かせること。
- 絵文字やハッシュタグは使わないこと。

# 参考例
「『失敗を恐れずに挑戦していいよ』って言った上司の顔が、本当に失敗したときに一番恐ろしかった。」
「新人教育っていうけど、教える側には1円の手当も出ずただただ自己犠牲とプレッシャーが増えるバグ、そろそろ修正してほしい。」
「『定時退社を推奨します』の横に、絶対に定時で終わらない量のタスクが積まれてるの何かの現代アート？」

# 出力形式
以下のキーのみを持つ JSON オブジェクトを1つだけ出力してください。
説明文やマークダウンのコードフェンスは付けないこと。

{
  "text": "生成した投稿本文（40〜100文字程度、日本語）",
  "image_prompt": "この投稿を象徴するシュールな風刺イラストを生成するための英語の画像生成プロンプト"
}

# image_prompt の作り方

image_prompt は「状況の説明」ではなく「感情のメタファー化」でなければなりません。
まず投稿テキストが伝えている感情（虚無感・ブラックさ・矛盾・あきらめ等）を1つ特定し、
その感情を誰が見ても意味の分かるシュールな一つの視覚的比喩に変換してください。
セリフ・文字・記号は一切使わず、絵だけで意味が伝わる無言の1コマ漫画にすること。

悪い例（状況をそのまま描写しているだけで比喩になっていない。禁止）:
- テキスト:「コンプライアンスを重視しすぎて上司が一言も指導しなくなった」
  ✗ 悪い例: 上司が黙って座っている。
  ✓ 良い例: 上司の口に頑丈な南京錠がかけられている。その隣で部下が笑顔でキーボードを叩いている。
- テキスト:「1on1（本音で話そう）の時間が早送りに感じる」
  ✗ 悪い例: オフィスで上司と部下が1on1をしている。
  ✓ 良い例: オフィスデスクで向き合う2人。上司の頭部が巨大な早送りボタン（⏩）になっている。

他の発想例（そのまま使わず、投稿内容に合わせて考案すること）:
- 「形だけの定時退社」→ 定時ダッシュする社員の足首に、タスクと書かれた鉄球付きの鎖が繋がれている
- 「手当のない新人教育」→ 後輩にライフバー（HP）を分け与えて、自分がスケルトンになりかけている先輩社員

image_prompt は曖昧な形容詞だけで済ませず、誰が読んでも同じ絵を思い浮かべられる
具体性が必須です。以下の5要素を、この順番で1つの英文にまとめてください。

- Subject: 誰が描かれるか（例: a Japanese office worker in a shirt and tie）
- Action: その主体が何をしているか（例: typing on a keyboard with a forced smile）
- Metaphor: 感情を表す視覚的比喩オブジェクト（例: a heavy padlock sealing the boss's mouth）
- Background: 場面設定（例: a minimal gray office cubicle, plain desk, single window）
- Style: 下記スタイルキーワードをそのまま使用

image_prompt の末尾には、次のスタイルキーワードを必ずそのまま含めてください
（一言一句変えないこと。画像内に文字や単語を一切描画させないための指定です）:
"Japanese single-panel satirical office webcomic, high contrast flat vector art, bold clean outlines, muted tones, surreal corporate life metaphor, silent comic style, no speech bubbles, strictly NO text, strictly NO words, strictly NO typography, strictly NO dialogue"
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


def generate_image(image_prompt: str, max_retries: int = IMAGE_MAX_RETRIES) -> bytes:
    """Pollinations.ai (FLUX) で風刺画像を生成する。APIキー不要。

    画像は必須のため、リトライしても取得できなかった場合は例外を送出して
    ワークフローを失敗させる（画像なしの Issue は作成しない）。
    """
    full_prompt = f"{image_prompt}, {ILLUSTRATION_STYLE}"
    encoded_prompt = urllib.parse.quote(full_prompt, safe="")

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        seed = random.randint(0, 2**31 - 1)
        url = (
            f"{POLLINATIONS_BASE_URL}/{encoded_prompt}"
            f"?width={IMAGE_WIDTH}&height={IMAGE_HEIGHT}&model={IMAGE_MODEL}"
            f"&nologo=true&seed={seed}"
        )
        print(
            f"[image] Pollinations.ai へリクエストします "
            f"(試行 {attempt}/{max_retries}, seed={seed})"
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
                f"[image] 試行 {attempt}/{max_retries} が失敗しました: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            last_error = exc
            if attempt < max_retries:
                wait_seconds = 2**attempt
                print(f"[image] {wait_seconds}秒待機してリトライします。")
                time.sleep(wait_seconds)
            continue

        print(
            f"[image] 画像を取得しました "
            f"（{len(response.content)} bytes、試行 {attempt}/{max_retries}）"
        )
        return response.content

    raise RuntimeError(
        f"Pollinations.ai への画像生成リクエストが{max_retries}回とも失敗しました"
    ) from last_error


def _find_caption_font_path() -> str:
    for path in CAPTION_FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    raise RuntimeError(
        "日本語フォントが見つかりません。GitHub Actions では "
        "`apt-get install fonts-noto-cjk` 等でインストールしてください。"
        f"探索したパス: {CAPTION_FONT_CANDIDATES}"
    )


def _shorten_caption(text: str) -> str:
    if len(text) <= CAPTION_MAX_CHARS:
        return text
    return text[: CAPTION_MAX_CHARS - 1] + "…"


def _wrap_japanese(text: str) -> list[str]:
    chars_per_line = CAPTION_MAX_CHARS_PER_LINE
    lines = [text[i : i + chars_per_line] for i in range(0, len(text), chars_per_line)]
    return lines[:CAPTION_MAX_LINES]


def add_japanese_caption(image_bytes: bytes, caption_text: str) -> bytes:
    """イラストの下部に半透明ブラックの帯を重ね、白文字で日本語テロップを合成する。"""
    font_path = _find_caption_font_path()
    image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    width, height = image.size

    lines = _wrap_japanese(_shorten_caption(caption_text))
    font_size = max(28, width // 18)
    font = ImageFont.truetype(font_path, font_size)

    measure_draw = ImageDraw.Draw(image)
    line_metrics = []
    for line in lines:
        left, top, right, bottom = measure_draw.textbbox((0, 0), line, font=font)
        line_metrics.append((line, right - left, bottom - top))

    line_spacing = int(font_size * 0.4)
    padding = int(font_size * 0.8)
    text_block_height = sum(h for _, _, h in line_metrics) + line_spacing * (len(lines) - 1)
    bar_height = text_block_height + padding * 2
    bar_top = height - bar_height

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle(
        [(0, bar_top), (width, height)], fill=(0, 0, 0, 160)
    )
    composited = Image.alpha_composite(image, overlay)
    draw = ImageDraw.Draw(composited)

    y = bar_top + padding
    for line, line_width, line_height in line_metrics:
        x = (width - line_width) / 2
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_height + line_spacing

    output = io.BytesIO()
    composited.convert("RGB").save(output, format="PNG")
    return output.getvalue()


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
    image_bytes = add_japanese_caption(image_bytes, draft["text"])
    print("日本語テロップを合成しました。")
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
