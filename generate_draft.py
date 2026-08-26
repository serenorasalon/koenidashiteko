"""
声なき多数派 (@koenidashiteko) 下書き＆テキストカード自動生成スクリプト。

Gemini API でペルソナに基づく「本音ぼやきテキスト」を生成し、Pillow で
そのテキストをカード画像として描画する。生成した画像は images/queue/ に
保存してリポジトリにコミット＆プッシュし、テキストと画像プレビューを
載せた GitHub Issue を作成する。
"""

import io
import json
import os
import re
import subprocess
import sys
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

JST = timezone(timedelta(hours=9))

# --- テキストカード描画設定 ---
CARD_WIDTH = 1200
CARD_HEIGHT = 1200
CARD_BACKGROUND_COLOR = (248, 249, 250)  # #F8F9FA
CARD_TEXT_COLOR = (26, 26, 26)  # 濃いグレー/黒
CARD_ACCOUNT_COLOR = (108, 117, 125)  # #6c757d
CARD_ACCOUNT_LABEL = "@koenidashiteko"
CARD_MARGIN = 120

# apt-get install fonts-noto-cjk（Ubuntu/GitHub Actions）で入る標準パスを優先し、
# 環境によって異なる ipaexfont のパッケージ配置や、ローカル Windows での動作
# 確認用に使えるフォントもフォールバックとして並べている。
CJK_FONT_CANDIDATES = [
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

PERSONA_PROMPT = """あなたはX（旧Twitter）の匿名アカウント「声なき多数派」(@koenidashiteko) の
中の人です。新人社員・アルバイト・20代若手が思わず「それな」「わかりすぎる」と
共感してクスッと笑える、身近でポップな日常あるあるを代弁する投稿を1つ
作成してください。難しい時事問題や組織論は扱わないこと。

このテキストは、そのまま正方形のテキストカード画像に大きく表示されます。
一目で読めて視認性が高くなるよう、短くキレのある言葉選びを徹底してください。

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
- 25〜45文字程度の短くポップな一言にすること。
- フォロワーへの問いかけ・アンケート・「〜ですよね？」のような質問形式は禁止。
  あくまで独り言・ぼやき・本音の吐露として書くこと。
- 説教くささ・小難しさはゼロにすること。ユーモア全開の「心の叫び・愛嬌のある
  ぼやき」にすること。ネガティブすぎず、「それな」「わかりすぎる」と思わず
  クスッと笑えるようにすること。
- 絵文字やハッシュタグは使わないこと。

# 参考例
「『何かあったら聞いてね』を信じて聞きに行ったら『今忙しい』は詐欺。」
「出勤前の布団、明らかに普段の5倍の引力で私を離してくれない。」
「給料日の3日後に残高を見る勇気、誰か私にください。」

# 出力形式
以下のキーのみを持つ JSON オブジェクトを1つだけ出力してください。
説明文やマークダウンのコードフェンスは付けないこと。

{
  "text": "生成した投稿本文（25〜45文字程度、日本語）"
}
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


def generate_text(client: genai.Client) -> str:
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
        if not text:
            raise ValueError(f"Gemini から空の値が返されました: {data!r}")

        print(f"[text] モデル '{model}' でテキストを生成しました。")
        return text

    raise RuntimeError(
        f"すべてのテキスト生成モデル候補 {candidates} で失敗しました"
    ) from last_error


def _find_cjk_font_path() -> str:
    for path in CJK_FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    raise RuntimeError(
        "日本語フォントが見つかりません。GitHub Actions では "
        "`apt-get install fonts-noto-cjk` 等でインストールしてください。"
        f"探索したパス: {CJK_FONT_CANDIDATES}"
    )


def _wrap_japanese(text: str, chars_per_line: int) -> list[str]:
    lines = [text[i : i + chars_per_line] for i in range(0, len(text), chars_per_line)]
    # 句点などが1文字だけで孤立して改行されるのを防ぎ、前の行にくっつける。
    if len(lines) > 1 and len(lines[-1]) == 1:
        lines[-2] += lines.pop()
    return lines


def _measure_lines(
    draw: ImageDraw.ImageDraw, lines: list[str], font: ImageFont.FreeTypeFont
) -> tuple[list[int], list[int]]:
    widths, heights = [], []
    for line in lines:
        left, top, right, bottom = draw.textbbox((0, 0), line, font=font)
        widths.append(right - left)
        heights.append(bottom - top)
    return widths, heights


def build_text_card(text: str) -> bytes:
    """ぼやきテキストを、キレのある一言テキストカード画像として描画する。"""
    font_path = _find_cjk_font_path()
    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), CARD_BACKGROUND_COLOR)
    draw = ImageDraw.Draw(image)

    max_block_width = CARD_WIDTH - CARD_MARGIN * 2
    max_block_height = CARD_HEIGHT - CARD_MARGIN * 2

    # テキストの長さに応じて折り返し幅とフォントサイズを自動調整し、
    # 余白に収まる最大サイズを採用する。
    chosen = None
    for chars_per_line, font_size in (
        (8, 108),
        (10, 92),
        (12, 78),
        (14, 66),
        (16, 56),
        (18, 48),
    ):
        font = ImageFont.truetype(font_path, font_size)
        lines = _wrap_japanese(text, chars_per_line)
        widths, heights = _measure_lines(draw, lines, font)
        line_spacing = int(font_size * 0.5)
        block_width = max(widths) if widths else 0
        block_height = sum(heights) + line_spacing * (len(lines) - 1)
        if block_width <= max_block_width and block_height <= max_block_height:
            chosen = (font, lines, widths, heights, line_spacing, block_height)
            break

    if chosen is None:
        # どのサイズでも収まらない極端に長いテキストは、最小サイズで強制描画する。
        chars_per_line, font_size = 18, 48
        font = ImageFont.truetype(font_path, font_size)
        lines = _wrap_japanese(text, chars_per_line)
        widths, heights = _measure_lines(draw, lines, font)
        line_spacing = int(font_size * 0.5)
        block_height = sum(heights) + line_spacing * (len(lines) - 1)
        chosen = (font, lines, widths, heights, line_spacing, block_height)

    font, lines, widths, heights, line_spacing, block_height = chosen

    y = (CARD_HEIGHT - block_height) / 2
    for line, line_width, line_height in zip(lines, widths, heights):
        x = (CARD_WIDTH - line_width) / 2
        draw.text((x, y), line, font=font, fill=CARD_TEXT_COLOR)
        y += line_height + line_spacing

    account_font = ImageFont.truetype(font_path, 34)
    left, top, right, bottom = draw.textbbox((0, 0), CARD_ACCOUNT_LABEL, font=account_font)
    account_width = right - left
    draw.text(
        (CARD_WIDTH - account_width - 60, CARD_HEIGHT - 90),
        CARD_ACCOUNT_LABEL,
        font=account_font,
        fill=CARD_ACCOUNT_COLOR,
    )

    output = io.BytesIO()
    image.save(output, format="PNG")
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
        f"## テキストカードプレビュー\n\n"
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

    text = generate_text(client)
    print(f"生成テキスト: {text}")

    image_bytes = build_text_card(text)
    image_path = save_image(image_bytes)
    print(f"テキストカードを保存しました: {image_path}")
    git_commit_and_push([image_path], f"chore: add draft image {os.path.basename(image_path)}")

    issue = create_issue(text, image_path)
    print(f"Issue を作成しました: {issue['html_url']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"エラーが発生しました: {exc}", file=sys.stderr)
        sys.exit(1)
