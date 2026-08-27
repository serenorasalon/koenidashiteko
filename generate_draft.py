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
import random
import re
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
from datetime import datetime, timezone, timedelta

import requests
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

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

# --- テキストカード描画設定（日常風景ムード + ポスター風レイアウト） ---
CARD_WIDTH = 1200
CARD_HEIGHT = 1200
CARD_TEXT_MARGIN = 110
# 背景の上に重ねる暗いフィルター（0-255、テキストの視認性を確保するため。約65%）。
CARD_OVERLAY_OPACITY = 165
# 行ごとのジャンプ率（サイズ差）と配色のメリハリ設定。
CARD_COLOR_WHITE = (255, 255, 255)
CARD_COLOR_ACCENT = (255, 230, 0)  # #FFE600 ビビッドなイエロー
CARD_FONT_SIZE_NORMAL = 88
CARD_FONT_SIZE_LARGE = 136
CARD_LINE_SPACING_RATIO = 0.35
CARD_MIN_SCALE = 0.4  # 収まらない場合に縮小する下限

# --- 背景写真（写真APIが失敗した場合は必ずローカル生成にフォールバックする） ---
# Unsplash Source (source.unsplash.com) は 2025 年に廃止されサービスが
# 停止しているため使用しない。代わりに、既にこのプロジェクトで実績のある
# Pollinations（APIキー不要）から写真的な背景画像を試みに取得する。
POLLINATIONS_BASE_URL = "https://image.pollinations.ai/prompt"
PHOTO_BACKGROUND_MODEL = "flux"
PHOTO_BACKGROUND_RETRIES = 2
PHOTO_BACKGROUND_KEYWORDS = [
    "nature landscape",
    "city skyline",
    "sky and clouds",
    "quiet street",
    "coffee shop window",
    "sunset",
    "rainy window",
    "morning light",
]

# 写真背景の取得に失敗した場合の最終フォールバック。外部の写真API（Unsplash等）
# には依存せず、Pillow だけで完結する日常のムードを表現する。ネットワーク
# 不要・APIキー不要で確実に動作させるため。
CARD_MOOD_THEMES = [
    {  # 夕暮れの街
        "name": "dusk_skyline",
        "sky_top": (255, 158, 92),
        "sky_bottom": (43, 27, 74),
        "silhouette": (18, 13, 26),
    },
    {  # オフィスの窓
        "name": "office_window",
        "sky_top": (68, 90, 122),
        "sky_bottom": (18, 24, 36),
        "silhouette": None,
    },
    {  # 雨の日の交差点
        "name": "rainy_crossing",
        "sky_top": (46, 58, 78),
        "sky_bottom": (12, 16, 24),
        "silhouette": None,
    },
    {  # 早朝の空
        "name": "early_morning",
        "sky_top": (255, 226, 214),
        "sky_bottom": (140, 172, 210),
        "silhouette": (54, 64, 84),
    },
]

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

# --- フォントバリエーション（毎回ランダムに1スタイルを選ぶ） ---
# 「怨霊フォント」はGoogle Fontsに実在を確認できなかったため採用していない。
# 以下はいずれも https://github.com/google/fonts (raw.githubusercontent.com)
# から実際にダウンロード可能なことを確認済みの実在フォントのみを使用する。
FONT_STYLE_GOTHIC = "gothic"  # 力強いインパクト（Noto Sans CJK、apt導入済み）
FONT_STYLE_HANDWRITTEN = "handwritten"  # 手書き風・脱力・エモい
FONT_STYLE_BRUSH = "brush"  # 明朝・筆文字、情緒・シリアスな切れ味
FONT_STYLES = (FONT_STYLE_GOTHIC, FONT_STYLE_HANDWRITTEN, FONT_STYLE_BRUSH)

DOWNLOADABLE_FONTS = {
    FONT_STYLE_HANDWRITTEN: [
        "https://raw.githubusercontent.com/google/fonts/main/ofl/kleeone/KleeOne-Regular.ttf",
        "https://raw.githubusercontent.com/google/fonts/main/ofl/yujiboku/YujiBoku-Regular.ttf",
    ],
    FONT_STYLE_BRUSH: [
        "https://raw.githubusercontent.com/google/fonts/main/ofl/shipporimincho/ShipporiMincho-Bold.ttf",
    ],
}

PERSONA_PROMPT = """あなたはX（旧Twitter）の匿名アカウント「声なき多数派」(@koenidashiteko) の
中の人です。新人社員・アルバイト・20代若手が思わず「それな」「わかりすぎる」と
共感してクスッと笑える、身近でポップな日常あるあるを代弁する投稿を1つ
作成してください。難しい時事問題や組織論は扱わないこと。

投稿は「tweet_intro（投稿本文＝導入・フック）」と「card_lines（画像カードに
描画する本音の核心・オチを、複数行に構造化したもの）」の2つに役割分担して
作成します。tweet_intro は画像を見たくなるような前振り・引きの一言、
card_lines はその答え・オチとなるキラーフレーズです。両方とも同じ1つの
テーマ・エピソードから作ること（別々の話題にしないこと）。

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

# 投稿のルール
- 説教くささ・小難しさはゼロにすること。ユーモア全開の「心の叫び・愛嬌のある
  ぼやき」にすること。ネガティブすぎず、「それな」「わかりすぎる」と思わず
  クスッと笑えるようにすること。
- 絵文字やハッシュタグは使わないこと。

## tweet_intro（投稿本文）のルール
- 15〜35文字程度の短い前振り・導入・共感を誘う引きの一言にすること。
- 「これ」「あの現象」のように画像の中身を直接明かさず、見たくなるように
  問いかけ・シチュエーション提示だけで留めること。フォロワーへの
  アンケート・「〜ですよね？」のような質問形式は禁止。

## card_lines（画像カード）のルール
card_lines は、本音の核心・オチとなるメッセージ全体（合わせて25〜45文字程度）を、
3〜4行の配列として構造化したものです。機械的な文字数折り返しは禁止し、
必ず「文節（意味のまとまり）」ごとに改行してください。「マットレス」を
「マット」「レス」のように、単語や複合語の途中で切ってはいけません。
各行はおおよそ8〜16文字程度の自然な長さにすること。

各行は以下のオブジェクトです:
- text: その行の文字列（文節単位）
- size: "normal" または "large"（大きく見せたい行のみ "large"）
- color: "white" または "accent"（強調したい行のみ "accent"）

メッセージの中で最も強調したい「キラーフレーズ・キーワード」を含む行だけを
1〜2行、size="large" かつ color="accent" にしてください。それ以外の行は
size="normal", color="white" にすること。全行を large/accent にするなど、
強調しすぎないこと。

# 参考例
tweet_intro:「バイト先で最も警戒すべき質問がこれ。」
card_lines:
  [
    {"text": "『明日休み？』と聞かれた瞬間", "size": "normal", "color": "white"},
    {"text": "起動する", "size": "normal", "color": "white"},
    {"text": "シフト代行警戒アラート。", "size": "large", "color": "accent"}
  ]

tweet_intro:「出勤前に必ず発生する、物理法則を無視した現象。」
card_lines:
  [
    {"text": "出勤前の布団の引力が", "size": "normal", "color": "white"},
    {"text": "普段の5倍になり、", "size": "large", "color": "accent"},
    {"text": "体がマットレスと一体化する", "size": "normal", "color": "white"},
    {"text": "絶望感。", "size": "large", "color": "accent"}
  ]

# 出力形式
以下のキーのみを持つ JSON オブジェクトを1つだけ出力してください。
説明文やマークダウンのコードフェンスは付けないこと。

{
  "tweet_intro": "生成した投稿本文（15〜35文字程度、日本語）",
  "card_lines": [
    {"text": "文節1", "size": "normal|large", "color": "white|accent"}
  ]
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


def _normalize_card_lines(raw_lines: list) -> list[dict]:
    normalized = []
    for item in raw_lines:
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        size = item.get("size") if item.get("size") in ("normal", "large") else "normal"
        color = item.get("color") if item.get("color") in ("white", "accent") else "white"
        normalized.append({"text": text, "size": size, "color": color})
    if not normalized:
        raise ValueError("card_lines が空、または有効な行がありませんでした")
    return normalized


def generate_draft_texts(client: genai.Client) -> dict:
    """tweet_intro（投稿本文＝導入）と card_lines（画像カードの構造化された核心）を生成する。"""
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

        tweet_intro = data["tweet_intro"].strip()
        card_lines = _normalize_card_lines(data["card_lines"])
        if not tweet_intro:
            raise ValueError(f"Gemini から空の値が返されました: {data!r}")

        print(f"[text] モデル '{model}' でテキストを生成しました。")
        return {"tweet_intro": tweet_intro, "card_lines": card_lines}

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


def _download_font(url: str) -> str | None:
    """フォントファイルをダウンロードし、ローカルパスを返す（失敗時は None）。

    同じ実行内で再利用できるよう一時ディレクトリにキャッシュする。
    """
    cache_dir = os.path.join(tempfile.gettempdir(), "koenidashiteko_fonts")
    os.makedirs(cache_dir, exist_ok=True)
    dest = os.path.join(cache_dir, url.rsplit("/", 1)[-1])

    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        if not response.content:
            raise RuntimeError("空のレスポンスでした")
        with open(dest, "wb") as f:
            f.write(response.content)
        return dest
    except Exception as exc:  # noqa: BLE001
        print(f"[font] フォントのダウンロードに失敗しました ({url}): {exc}", file=sys.stderr)
        return None


def _resolve_font_path(style: str) -> str:
    """指定スタイルのフォントパスを返す。ダウンロード失敗時はゴシックに
    フォールバックするため、この関数が例外を送出することはない
    （フォント取得はあくまで見た目の演出であり、カード生成自体は
    絶対に止めないため）。
    """
    urls = list(DOWNLOADABLE_FONTS.get(style, []))
    random.shuffle(urls)
    for url in urls:
        path = _download_font(url)
        if path:
            return path
    if urls:
        print(
            f"[font] スタイル '{style}' のフォント取得にすべて失敗したため、"
            "ゴシック体にフォールバックします。",
            file=sys.stderr,
        )
    return _find_cjk_font_path()


def _choose_font_style() -> str:
    return random.choice(FONT_STYLES)


def _make_vertical_gradient(
    width: int, height: int, color_top: tuple, color_bottom: tuple
) -> Image.Image:
    base = Image.new("RGB", (width, height), color_top)
    overlay = Image.new("RGB", (width, height), color_bottom)
    mask = Image.new("L", (width, height))
    mask.putdata([int(255 * (y / height)) for y in range(height) for _ in range(width)])
    return Image.composite(overlay, base, mask)


def _draw_skyline_silhouette(image: Image.Image, color: tuple) -> None:
    """夕暮れ・早朝テーマ向けに、下部にビル群のシルエットを描く。"""
    width, height = image.size
    draw = ImageDraw.Draw(image)
    base_y = int(height * 0.74)
    x = 0
    while x < width:
        block_width = random.randint(70, 160)
        block_height = random.randint(int(height * 0.06), int(height * 0.24))
        draw.rectangle(
            [x, base_y - block_height, x + block_width, height], fill=(*color, 255)
        )
        x += block_width + random.randint(4, 16)


def _draw_window_grid(image: Image.Image) -> None:
    """オフィスの窓テーマ向けに、ぼんやり灯る窓明かりのグリッドを描く。"""
    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    cols, rows = 8, 10
    cell_w, cell_h = width / cols, height / rows
    for row in range(rows):
        for col in range(cols):
            if random.random() < 0.4:
                left = col * cell_w + cell_w * 0.15
                top = row * cell_h + cell_h * 0.15
                right = left + cell_w * 0.7
                bottom = top + cell_h * 0.7
                alpha = random.randint(10, 35)
                draw.rectangle([left, top, right, bottom], fill=(255, 238, 200, alpha))
    image.alpha_composite(overlay)


def _draw_rain_streaks(image: Image.Image) -> None:
    """雨の日テーマ向けに、細い雨粒の筋を散らす。"""
    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for _ in range(140):
        x = random.randint(0, width)
        y = random.randint(0, height)
        length = random.randint(30, 90)
        alpha = random.randint(15, 45)
        draw.line(
            [(x, y), (x - int(length * 0.2), y + length)],
            fill=(205, 218, 232, alpha),
            width=2,
        )
    image.alpha_composite(overlay)


def _draw_bokeh_lights(image: Image.Image) -> None:
    """雨の日テーマ向けに、街灯の淡いにじみ（ボケ光）を加える。"""
    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    colors = [(255, 200, 120), (255, 150, 150), (150, 200, 255)]
    for _ in range(6):
        cx = random.randint(0, width)
        cy = random.randint(int(height * 0.6), height)
        r = random.randint(30, 70)
        color = random.choice(colors)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*color, 90))
    overlay = overlay.filter(ImageFilter.GaussianBlur(24))
    image.alpha_composite(overlay)


def _draw_horizon_glow(image: Image.Image) -> None:
    """早朝テーマ向けに、地平線あたりに柔らかい光の滲みを加える。"""
    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    cx, cy = width / 2, int(height * 0.62)
    r = int(width * 0.32)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 246, 224, 110))
    overlay = overlay.filter(ImageFilter.GaussianBlur(40))
    image.alpha_composite(overlay)


def _fetch_photo_background(width: int, height: int) -> Image.Image:
    """Pollinations から日常の雰囲気を感じさせる写真的な背景画像を取得する。

    失敗した場合は例外を送出する。呼び出し元は必ず `_make_scenic_background`
    へフォールバックすること（写真取得はあくまで演出であり、これが失敗しても
    カード生成自体は絶対に止めないため）。
    """
    keyword = random.choice(PHOTO_BACKGROUND_KEYWORDS)
    prompt = (
        f"{keyword}, atmospheric everyday life photography, cinematic natural "
        f"lighting, high detail, photorealistic"
    )
    encoded_prompt = urllib.parse.quote(prompt, safe="")

    last_error: Exception | None = None
    for attempt in range(1, PHOTO_BACKGROUND_RETRIES + 1):
        seed = random.randint(0, 2**31 - 1)
        url = (
            f"{POLLINATIONS_BASE_URL}/{encoded_prompt}"
            f"?width={width}&height={height}&model={PHOTO_BACKGROUND_MODEL}"
            f"&nologo=true&seed={seed}"
        )
        try:
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if not content_type.startswith("image/") or not response.content:
                raise RuntimeError(
                    f"画像以外のレスポンスでした (Content-Type: {content_type!r})"
                )
            photo = Image.open(io.BytesIO(response.content)).convert("RGBA")
            if photo.size != (width, height):
                photo = ImageOps.fit(photo, (width, height))
            print(f"[background] 写真背景を取得しました（keyword={keyword!r}）。")
            return photo
        except Exception as exc:  # noqa: BLE001
            print(
                f"[background] 写真背景の取得に失敗しました "
                f"(試行 {attempt}/{PHOTO_BACKGROUND_RETRIES}): {exc}",
                file=sys.stderr,
            )
            last_error = exc

    raise RuntimeError("写真背景の取得にすべて失敗しました") from last_error


def _make_scenic_background(width: int, height: int) -> Image.Image:
    """日常のワンシーンを感じさせるムード背景を、写真APIなしで手続き的に生成する。"""
    theme = random.choice(CARD_MOOD_THEMES)
    image = _make_vertical_gradient(
        width, height, theme["sky_top"], theme["sky_bottom"]
    ).convert("RGBA")

    if theme["name"] == "dusk_skyline":
        _draw_skyline_silhouette(image, theme["silhouette"])
    elif theme["name"] == "office_window":
        _draw_window_grid(image)
    elif theme["name"] == "rainy_crossing":
        _draw_rain_streaks(image)
        _draw_bokeh_lights(image)
    elif theme["name"] == "early_morning":
        _draw_horizon_glow(image)
        _draw_skyline_silhouette(image, theme["silhouette"])

    return image


def _resolve_line_style(line: dict) -> tuple[int, tuple]:
    font_size = CARD_FONT_SIZE_LARGE if line["size"] == "large" else CARD_FONT_SIZE_NORMAL
    color = CARD_COLOR_ACCENT if line["color"] == "accent" else CARD_COLOR_WHITE
    return font_size, color


def _layout_card_lines(
    draw: ImageDraw.ImageDraw, font_path: str, card_lines: list[dict], scale: float
) -> tuple[list[tuple], int, int]:
    """指定スケールで各行を計測し、(描画情報リスト, 最大幅, 合計高さ) を返す。"""
    rendered = []
    for line in card_lines:
        base_size, color = _resolve_line_style(line)
        font_size = max(16, int(base_size * scale))
        font = ImageFont.truetype(font_path, font_size)
        left, top, right, bottom = draw.textbbox((0, 0), line["text"], font=font)
        width, height = right - left, bottom - top
        rendered.append((line["text"], font, color, width, height))

    max_width = max((r[3] for r in rendered), default=0)
    max_line_height = max((r[4] for r in rendered), default=0)
    line_spacing = int(max_line_height * CARD_LINE_SPACING_RATIO)
    total_height = sum(r[4] for r in rendered) + line_spacing * (len(rendered) - 1)
    return rendered, max_width, total_height, line_spacing


def build_text_card(card_lines: list[dict]) -> bytes:
    """card_lines を、日常風景ムードの背景に載せたポスター風カードとして描画する。

    Gemini が意味のまとまり（文節）ごとに改行・強調指定した行をそのまま使い、
    ここでは単語途中の再折り返しは行わない。行が余白に収まらない場合のみ、
    行の区切りを保ったまま全体を比例縮小する安全策を取る。
    """
    font_style = _choose_font_style()
    font_path = _resolve_font_path(font_style)
    print(f"[font] 使用フォントスタイル: {font_style} ({font_path})")

    try:
        image = _fetch_photo_background(CARD_WIDTH, CARD_HEIGHT)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[background] 写真背景を断念し、ローカル生成の背景にフォールバックします: {exc}",
            file=sys.stderr,
        )
        image = _make_scenic_background(CARD_WIDTH, CARD_HEIGHT)

    # 中央の白文字がくっきり読めるよう、暗いフィルターを全体に重ねてコントラストを確保する。
    dark_overlay = Image.new("RGBA", image.size, (0, 0, 0, CARD_OVERLAY_OPACITY))
    image = Image.alpha_composite(image, dark_overlay)

    draw = ImageDraw.Draw(image)

    max_block_width = CARD_WIDTH - CARD_TEXT_MARGIN * 2
    max_block_height = CARD_HEIGHT - CARD_TEXT_MARGIN * 2

    scale = 1.0
    while True:
        rendered, block_width, block_height, line_spacing = _layout_card_lines(
            draw, font_path, card_lines, scale
        )
        if block_width <= max_block_width and block_height <= max_block_height:
            break
        if scale <= CARD_MIN_SCALE:
            break
        scale -= 0.05

    y = (CARD_HEIGHT - block_height) / 2
    for text, font, color, line_width, line_height in rendered:
        x = (CARD_WIDTH - line_width) / 2
        draw.text((x, y), text, font=font, fill=color)
        y += line_height + line_spacing

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


def create_issue(tweet_intro: str, card_lines: list[dict], image_path: str) -> dict:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    branch = os.environ.get("GITHUB_REF_NAME", "main")

    raw_image_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{image_path}"
    # post_approved.py は meta["text"] をそのまま X の投稿本文として使う。
    # card_lines は画像に焼き込み済みのため meta には含めない。
    meta = json.dumps({"text": tweet_intro, "image_path": image_path}, ensure_ascii=False)

    card_preview = "\n".join(
        f"> {'**' if line['size'] == 'large' else ''}{line['text']}"
        f"{'**' if line['size'] == 'large' else ''}"
        f"{'（accent）' if line['color'] == 'accent' else ''}"
        for line in card_lines
    )

    body = (
        f"## 投稿テキスト（導入・フック）\n\n"
        f"> {tweet_intro}\n\n"
        f"## 画像カードメッセージ（本音の核心・行構成）\n\n"
        f"{card_preview}\n\n"
        f"## テキストカードプレビュー\n\n"
        f"![draft image]({raw_image_url})\n\n"
        f"---\n"
        f"このIssueに `approved` ラベルを付けると自動投稿されます。\n\n"
        f"<!--KOENIDASHITEKO_META\n{meta}\n-->\n"
    )

    now_jst = datetime.now(JST)
    title = f"[下書き] {now_jst.strftime('%Y-%m-%d %H:%M')} JST - {tweet_intro[:20]}"

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

    draft = generate_draft_texts(client)
    print(f"投稿テキスト（導入）: {draft['tweet_intro']}")
    print(f"画像カード行構成（核心）: {draft['card_lines']}")

    image_bytes = build_text_card(draft["card_lines"])
    image_path = save_image(image_bytes)
    print(f"テキストカードを保存しました: {image_path}")
    git_commit_and_push([image_path], f"chore: add draft image {os.path.basename(image_path)}")

    issue = create_issue(draft["tweet_intro"], draft["card_lines"], image_path)
    print(f"Issue を作成しました: {issue['html_url']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"エラーが発生しました: {exc}", file=sys.stderr)
        sys.exit(1)
