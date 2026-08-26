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
from PIL import Image, ImageDraw, ImageFont, ImageFilter

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

# --- テキストカード描画設定（プロデザイン調ダークテーマ） ---
CARD_WIDTH = 1200
CARD_HEIGHT = 1200
CARD_BG_TOP = (18, 22, 26)  # #12161A
CARD_BG_BOTTOM = (30, 35, 42)  # #1E232A
CARD_PANEL_MARGIN = 56
CARD_PANEL_RADIUS = 40
CARD_ACCENT_COLOR = (77, 224, 189)  # 発光ボーダー・バッジに使うアクセントカラー
CARD_TEXT_COLOR = (255, 255, 255)
CARD_FOOTER_COLOR = (150, 160, 172)
CARD_FOOTER_LABEL = "声なき多数派  |  @koenidashiteko"
CARD_BADGE_LABEL = "VOICE OF SILENT MAJORITY"
CARD_QUOTE_MARK = "“"

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
        last = lines.pop()
        lines[-1] += last
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


def _make_vertical_gradient(
    width: int, height: int, color_top: tuple, color_bottom: tuple
) -> Image.Image:
    base = Image.new("RGB", (width, height), color_top)
    overlay = Image.new("RGB", (width, height), color_bottom)
    mask = Image.new("L", (width, height))
    mask.putdata([int(255 * (y / height)) for y in range(height) for _ in range(width)])
    return Image.composite(overlay, base, mask)


def _draw_rounded_panel_with_glow(base: Image.Image) -> None:
    """中央に角丸パネルを描き、外周にアクセントカラーの微かな発光ボーダーを重ねる。"""
    box = (
        CARD_PANEL_MARGIN,
        CARD_PANEL_MARGIN,
        CARD_WIDTH - CARD_PANEL_MARGIN,
        CARD_HEIGHT - CARD_PANEL_MARGIN,
    )

    # 発光レイヤー: パネル外周と同じ角丸ボーダーをぼかして下敷きにする。
    glow_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    glow_draw.rounded_rectangle(
        box, radius=CARD_PANEL_RADIUS, outline=(*CARD_ACCENT_COLOR, 140), width=6
    )
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(14))
    base.alpha_composite(glow_layer)

    # パネル本体（背景よりわずかに明るいダークトーン）とシャープな境界線。
    panel_draw = ImageDraw.Draw(base)
    panel_fill = tuple(min(255, c + 6) for c in CARD_BG_BOTTOM)
    panel_draw.rounded_rectangle(
        box, radius=CARD_PANEL_RADIUS, fill=(*panel_fill, 255)
    )
    panel_draw.rounded_rectangle(
        box, radius=CARD_PANEL_RADIUS, outline=(*CARD_ACCENT_COLOR, 200), width=2
    )


def _draw_giant_quote_mark(base: Image.Image, font_path: str) -> None:
    """奥行きを出すための、半透明の巨大な引用符を左上寄りに配置する。"""
    quote_font = ImageFont.truetype(font_path, 620)
    quote_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    quote_draw = ImageDraw.Draw(quote_layer)
    quote_draw.text(
        (CARD_PANEL_MARGIN + 20, CARD_PANEL_MARGIN - 60),
        CARD_QUOTE_MARK,
        font=quote_font,
        fill=(255, 255, 255, 28),  # 約11%の不透明度
    )
    base.alpha_composite(quote_layer)


def _draw_spaced_text(
    draw: ImageDraw.ImageDraw,
    center_x: int,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill,
    letter_spacing: int = 5,
) -> None:
    """英字バッジ用に、文字間を少し空けた読みやすいレタースペーシングで描く。"""
    widths = [draw.textbbox((0, 0), ch, font=font)[2] for ch in text]
    total_width = sum(widths) + letter_spacing * (len(text) - 1)
    x = center_x - total_width / 2
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=fill)
        x += w + letter_spacing


def _draw_badge(base: Image.Image, font_path: str) -> None:
    draw = ImageDraw.Draw(base)
    badge_font = ImageFont.truetype(font_path, 24)
    letter_spacing = 5
    widths = [draw.textbbox((0, 0), ch, font=badge_font)[2] for ch in CARD_BADGE_LABEL]
    text_width = sum(widths) + letter_spacing * (len(CARD_BADGE_LABEL) - 1)

    pad_x, pad_y = 28, 16
    pill_width = text_width + pad_x * 2
    pill_height = 24 + pad_y * 2
    pill_left = (CARD_WIDTH - pill_width) / 2
    pill_top = CARD_PANEL_MARGIN + 56
    pill_box = (pill_left, pill_top, pill_left + pill_width, pill_top + pill_height)

    draw.rounded_rectangle(pill_box, radius=pill_height / 2, fill=(*CARD_ACCENT_COLOR, 255))
    _draw_spaced_text(
        draw,
        CARD_WIDTH / 2,
        pill_top + pad_y,
        CARD_BADGE_LABEL,
        badge_font,
        fill=tuple(CARD_BG_TOP),
        letter_spacing=letter_spacing,
    )
    return pill_top + pill_height


def build_text_card(text: str) -> bytes:
    """ぼやきテキストを、プロデザイン調のダークテーマグラフィックカードとして描画する。"""
    font_path = _find_cjk_font_path()

    background = _make_vertical_gradient(CARD_WIDTH, CARD_HEIGHT, CARD_BG_TOP, CARD_BG_BOTTOM)
    image = background.convert("RGBA")

    _draw_rounded_panel_with_glow(image)
    _draw_giant_quote_mark(image, font_path)
    badge_bottom = _draw_badge(image, font_path)

    draw = ImageDraw.Draw(image)
    footer_font = ImageFont.truetype(font_path, 30)
    footer_top = CARD_HEIGHT - CARD_PANEL_MARGIN - 76

    text_area_top = badge_bottom + 40
    text_area_bottom = footer_top - 40
    max_block_width = CARD_WIDTH - (CARD_PANEL_MARGIN + 100) * 2
    max_block_height = text_area_bottom - text_area_top

    # テキストの長さに応じて折り返し幅とフォントサイズを自動調整し、
    # 余白に収まる最大サイズを採用する。
    chosen = None
    for chars_per_line, font_size in (
        (8, 92),
        (10, 80),
        (12, 68),
        (14, 58),
        (16, 50),
        (18, 44),
    ):
        font = ImageFont.truetype(font_path, font_size)
        lines = _wrap_japanese(text, chars_per_line)
        widths, heights = _measure_lines(draw, lines, font)
        line_spacing = int(font_size * 0.55)
        block_width = max(widths) if widths else 0
        block_height = sum(heights) + line_spacing * (len(lines) - 1)
        if block_width <= max_block_width and block_height <= max_block_height:
            chosen = (font, lines, widths, heights, line_spacing, block_height)
            break

    if chosen is None:
        # どのサイズでも収まらない極端に長いテキストは、最小サイズで強制描画する。
        font = ImageFont.truetype(font_path, 44)
        lines = _wrap_japanese(text, 18)
        widths, heights = _measure_lines(draw, lines, font)
        line_spacing = int(44 * 0.55)
        block_height = sum(heights) + line_spacing * (len(lines) - 1)
        chosen = (font, lines, widths, heights, line_spacing, block_height)

    font, lines, widths, heights, line_spacing, block_height = chosen

    y = text_area_top + (max_block_height - block_height) / 2
    for line, line_width, line_height in zip(lines, widths, heights):
        x = (CARD_WIDTH - line_width) / 2
        draw.text((x, y), line, font=font, fill=CARD_TEXT_COLOR)
        y += line_height + line_spacing

    footer_bbox = draw.textbbox((0, 0), CARD_FOOTER_LABEL, font=footer_font)
    footer_width = footer_bbox[2] - footer_bbox[0]
    draw.text(
        ((CARD_WIDTH - footer_width) / 2, footer_top),
        CARD_FOOTER_LABEL,
        font=footer_font,
        fill=CARD_FOOTER_COLOR,
    )

    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG")
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
