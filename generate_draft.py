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

# 曜日ごとの投稿テーマ（datetime.weekday(): 月=0 ... 日=6）。
# 日本時間（JST）基準で当日の曜日を判定し、Gemini への生成プロンプトに
# その日のテーマを明示的に注入することで、投稿内容を曜日ごとに切り替える。
WEEKDAY_THEMES = {
    0: "新人社会人・若手社員に関する本音・あるある・理不尽",
    1: "政治・社会制度・税金・世論に対する庶民の本音・チクリとした風刺",
    2: "ベテラン社員・管理職・おじさん世代の哀愁や本音・社内政治",
    3: "世間で話題になっている時事ニュース・トレンド・社会現象に関する本音",
    4: "新人社員・若手の週末直前の本音・解放感・ギャップ",
    5: "独身男性のリアルな日常・休日・生態・孤独と自由",
    6: "独身女性のリアルな休日・本音・日常のモヤモヤ・月曜前の心理",
}


def get_today_theme(now: datetime | None = None) -> str:
    """JST基準の当日の曜日から、本日投稿すべきテーマを返す。"""
    now = now or datetime.now(JST)
    return WEEKDAY_THEMES[now.weekday()]

# --- テキストカード描画設定（日常風景ムード + ポスター風レイアウト） ---
CARD_WIDTH = 1200
CARD_HEIGHT = 1200
CARD_TEXT_MARGIN = 110
# 背景の上に重ねる暗いフィルター（0-255、テキストの視認性を確保するため。約65%）。
CARD_OVERLAY_OPACITY = 165
# 行ごとのジャンプ率（サイズ差）と配色のメリハリ設定。
# 振り（normal）は控えめなオフホワイト、落とし（large/accent）は墨に映える
# 山吹金。素の白(#FFFFFF)/ビビッドイエロー(#FFE600)より、筆文字カードとして
# 落ち着きと重厚感が出る配色に調整している。
CARD_COLOR_WHITE = (240, 240, 240)  # #F0F0F0
CARD_COLOR_ACCENT = (255, 215, 0)  # #FFD700 山吹金
CARD_COLOR_INK = (18, 14, 12)  # 毛筆の縁取り・影に使う墨色
CARD_FONT_SIZE_NORMAL = 68
CARD_FONT_SIZE_LARGE = 150
CARD_MAX_LINES = 3  # 超短文構成のため、行数の安全上限
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

# --- ハイブリッドタイポグラフィ用フォント（行の役割ごとに固定で使い分ける） ---
# 「振り」の行（size="normal"）には端正な明朝体、「落とし」の行
# （size="large"、オチ・キラーフレーズ）には迫力ある毛筆体を使う。
#
# 「衡山毛筆フォント（KouzanBrushFont）」はGoogle Fontsに実在せず、配布元が
# CIから安定してダウンロードできる保証がない（このプロジェクトでは以前
# 「怨霊フォント」でも同様の理由で採用を見送っている）ため使用しない。
# 代わりに、いずれも https://github.com/google/fonts (raw.githubusercontent.com)
# から実際にダウンロード可能なことを確認済みの実在フォントのみを使用する。
CARD_FONT_URLS_NORMAL = [
    "https://raw.githubusercontent.com/google/fonts/main/ofl/shipporimincho/ShipporiMincho-Bold.ttf",
]
CARD_FONT_URLS_BRUSH = [
    "https://raw.githubusercontent.com/google/fonts/main/ofl/yujiboku/YujiBoku-Regular.ttf",
    "https://raw.githubusercontent.com/google/fonts/main/ofl/reggaeone/ReggaeOne-Regular.ttf",
]

PERSONA_PROMPT_HEADER = """あなたはX（旧Twitter）の匿名アカウント「声なき多数派」(@koenidashiteko) の
中の人です。「本音を言えない世の中の代弁者」として、世の中の様々な立場の
人が0.5秒で「わかる」と刺さる、超短文・インパクト重視の投稿を1つ
作成してください。難しい時事問題や組織論、長い状況説明やポエム調は
完全に禁止です。言葉を極限まで研ぎ澄ませること。

投稿は「tweet_intro（投稿本文＝導入・フック）」と「card_lines（画像カードに
描画する本音の核心・オチを、複数行に構造化したもの）」の2つに役割分担して
作成します。tweet_intro は画像を見たくなるような前振り・引きの一言、
card_lines はその答え・オチとなるキラーフレーズです。両方とも同じ1つの
テーマ・エピソードから作ること（別々の話題にしないこと）。
"""


def _build_theme_section(theme: str) -> str:
    """本日のテーマ（曜日別）を明示的に注入するプロンプト断片を作る。"""
    return (
        "# 本日のテーマ（最優先で厳守すること。他の曜日向けの話題や、"
        "このテーマから外れる題材は選ばないこと）\n"
        f"本日のテーマ: {theme}\n\n"
        "このテーマに基づき、声に出せないリアルな本音・不条理・共感の瞬間を、"
        "1行目の振り（日常の状況）と2行目の落とし（強烈な本音・オチ）の"
        "超短文構成で作成してください。\n"
    )


PERSONA_PROMPT_RULES = """# 投稿のルール
- 説教くささ・小難しさはゼロにすること。ユーモア全開の「心の叫び・愛嬌のある
  ぼやき」にすること。ネガティブすぎず、「それな」「わかりすぎる」と思わず
  クスッと笑えるようにすること。
- 絵文字やハッシュタグは使わないこと。

## tweet_intro（投稿本文）のルール
- **10〜18文字以内**の極短フレーズにすること（スクロールの手を止める引き）。
  長い前置きや状況説明は禁止。
- 「これ」「あの現象」のように画像の中身を直接明かさず、見たくなるように
  一言だけで留めること。フォロワーへのアンケート・「〜ですよね？」のような
  質問形式は禁止。
- （例）「全社会人が共感するホラー。」「これ以上の心理戦を知らない。」
  「誰も声に出せない真実。」「今世紀最大の無駄時間。」

## card_lines（画像カード）のルール
card_lines は、本音の核心・オチとなるメッセージ全体（合わせて**15〜25文字
程度**）を、**2〜3行**の配列として構造化したものです。1行目は「振り」
（本日のテーマに沿った日常の状況）、最後の行は「落とし」（強烈な本音・
オチ）に対応させること。長い説明文は厳禁、体言止め中心の言い切り
キラーフレーズにすること。機械的な文字数折り返しは禁止し、必ず「文節
（意味のまとまり）」ごとに改行してください。単語や複合語の途中で切っては
いけません。

各行は以下のオブジェクトです:
- text: その行の文字列（文節単位）
- size: "normal" または "large"（「落とし」の行のみ "large"）
- color: "white" または "accent"（強調したい行のみ "accent"）

「落とし」の行（メッセージの中で最も強調したいキラーフレーズ・キーワード
を含む行）だけを size="large" かつ color="accent" にしてください。それ
以外の行（「振り」）は size="normal", color="white" にすること。

# 参考例（文体・フォーマットのみの参考。内容は本日のテーマに関わらない
例示のため、実際の内容は必ず本日のテーマに沿ったものにすること）
tweet_intro:「これ以上の心理戦を知らない。」
card_lines:
  [
    {"text": "鳴り響く電話、", "size": "normal", "color": "white"},
    {"text": "始まる心理戦。", "size": "large", "color": "accent"}
  ]

tweet_intro:「今世紀最大の無駄時間。」
card_lines:
  [
    {"text": "出勤前の布団、", "size": "normal", "color": "white"},
    {"text": "重力5倍。", "size": "large", "color": "accent"}
  ]

tweet_intro:「誰も声に出せない真実。」
card_lines:
  [
    {"text": "「本音で話そう」という", "size": "normal", "color": "white"},
    {"text": "嘘のつき合い。", "size": "large", "color": "accent"}
  ]

tweet_intro:「全社会人が共感するホラー。」
card_lines:
  [
    {"text": "「明日休み？」という", "size": "normal", "color": "white"},
    {"text": "出勤確定宣告。", "size": "large", "color": "accent"}
  ]

# 出力形式
以下のキーのみを持つ JSON オブジェクトを1つだけ出力してください。
説明文やマークダウンのコードフェンスは付けないこと。

{
  "tweet_intro": "生成した投稿本文（10〜18文字以内、日本語）",
  "card_lines": [
    {"text": "文節1", "size": "normal|large", "color": "white|accent"}
  ]
}
"""


def build_persona_prompt(theme: str) -> str:
    """曜日別テーマを注入した、その日の生成用プロンプト全文を組み立てる。"""
    return PERSONA_PROMPT_HEADER + "\n" + _build_theme_section(theme) + "\n" + PERSONA_PROMPT_RULES


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
    # 超短文構成のため、行数が想定を超えて返ってきた場合の安全上限。
    return normalized[:CARD_MAX_LINES]


def generate_draft_texts(client: genai.Client) -> dict:
    """tweet_intro（投稿本文＝導入）と card_lines（画像カードの構造化された核心）を生成する。

    日本時間（JST）基準の当日の曜日から曜日別テーマを決定し、生成プロンプトに
    明示的に注入することで、投稿内容を曜日ごとに切り替える。
    """
    theme = get_today_theme()
    print(f"[theme] 本日のテーマ: {theme}")
    prompt = build_persona_prompt(theme)

    candidates = build_text_model_candidates(client)
    print(f"[text] モデル候補（優先順）: {candidates}")
    last_error: Exception | None = None

    for model in candidates:
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
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


def _resolve_font_path(role: str, urls: list[str]) -> str:
    """指定ロール（normal/brush）のフォントパスを返す。ダウンロード失敗時は
    ゴシックにフォールバックするため、この関数が例外を送出することはない
    （フォント取得はあくまで見た目の演出であり、カード生成自体は
    絶対に止めないため）。
    """
    shuffled = list(urls)
    random.shuffle(shuffled)
    for url in shuffled:
        path = _download_font(url)
        if path:
            return path
    if shuffled:
        print(
            f"[font] ロール '{role}' のフォント取得にすべて失敗したため、"
            "ゴシック体にフォールバックします。",
            file=sys.stderr,
        )
    return _find_cjk_font_path()


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


def _resolve_line_style(line: dict) -> tuple[int, tuple, bool]:
    """行の役割（振り/落とし）から、フォントサイズ・色・毛筆ロールかどうかを返す。

    「落とし」の行（size="large"、オチ・キラーフレーズ）だけを毛筆体・金色の
    特大サイズにし、それ以外の「振り」の行は明朝体・オフホワイトの通常
    サイズにする、というハイブリッドタイポグラフィのルールの中枢。
    """
    is_brush = line["size"] == "large"
    font_size = CARD_FONT_SIZE_LARGE if is_brush else CARD_FONT_SIZE_NORMAL
    color = CARD_COLOR_ACCENT if line["color"] == "accent" else CARD_COLOR_WHITE
    return font_size, color, is_brush


def _layout_card_lines(
    card_lines: list[dict], normal_font_path: str, brush_font_path: str, scale: float
) -> tuple[list[dict], int, int, int]:
    """指定スケールで各行を計測し、(描画情報リスト, 最大幅, 合計高さ, 行間) を返す。

    行の高さは textbbox の見た目上のバウンディングボックスではなく
    `font.getmetrics()` の ascent/descent を基準にする。毛筆フォントは
    デザイン上の内部余白（サイドベアリング）が明朝体と大きく異なるため、
    bbox基準だと振り・落とし間の行間が不揃いに見えてしまうのを防ぐため。
    """
    rendered = []
    for line in card_lines:
        base_size, color, is_brush = _resolve_line_style(line)
        font_path = brush_font_path if is_brush else normal_font_path
        font_size = max(16, int(base_size * scale))
        font = ImageFont.truetype(font_path, font_size)
        ascent, descent = font.getmetrics()
        left, top, right, bottom = font.getbbox(line["text"])
        width = right - left
        rendered.append(
            {
                "text": line["text"],
                "font": font,
                "color": color,
                "is_brush": is_brush,
                "width": width,
                "ascent": ascent,
                "descent": descent,
            }
        )

    max_width = max((r["width"] for r in rendered), default=0)
    # 行間の基準は「振り」の行の行高にする。毛筆フォントは行高が
    # 大きく振れがちで、それを基準にすると行間が間延びするため。
    normal_heights = [r["ascent"] + r["descent"] for r in rendered if not r["is_brush"]]
    all_heights = [r["ascent"] + r["descent"] for r in rendered]
    base_line_height = max(normal_heights or all_heights, default=0)
    line_spacing = int(base_line_height * CARD_LINE_SPACING_RATIO)
    total_height = sum(all_heights) + line_spacing * (len(rendered) - 1)
    return rendered, max_width, total_height, line_spacing


def _add_center_bottom_vignette(image: Image.Image) -> Image.Image:
    """毛筆文字の可読性を極限まで高めるため、カード中央〜下部に向かって
    暗くなる黒グラデーション（ヴィネット）を重ねる。"""
    width, height = image.size
    start = 0.28  # この高さ比率より上は暗くしない
    max_alpha = 200
    alpha_row = []
    for y in range(height):
        t = max(0.0, (y / height - start) / (1 - start))
        alpha_row.append(int(max_alpha * (t**1.3)))

    vignette = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    vignette.putdata(
        [(0, 0, 0, alpha_row[y]) for y in range(height) for _ in range(width)]
    )
    return Image.alpha_composite(image, vignette)


def _draw_card_line(image: Image.Image, line: dict, x: float, baseline_y: float) -> None:
    """1行分のテキストを描画する。

    振りの行: 控えめな黒のドロップシャドウ＋オフホワイトの本文。
    落としの行（毛筆）: 墨がにじむような多重の柔らかい影＋太めの黒縁取り＋
    山吹金の本文で、写真から浮き上がるようなインパクトを出す。
    どちらも anchor="ms"（水平中央・垂直ベースライン）で描画するため、
    フォントが異なっても各行のベースラインが揃った状態で積み上げられる。
    """
    text, font, color = line["text"], line["font"], line["color"]

    if line["is_brush"]:
        stroke_width = max(4, font.size // 16)

        # 1) 墨のにじみのような、ぼかした多重シャドウで奥行きを出す。
        glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        for dx, dy, alpha in ((0, font.size * 0.09, 130), (font.size * 0.03, font.size * 0.03, 90)):
            glow_draw.text(
                (x + dx, baseline_y + dy),
                text,
                font=font,
                anchor="ms",
                fill=(*CARD_COLOR_INK, alpha),
                stroke_width=stroke_width,
                stroke_fill=(*CARD_COLOR_INK, alpha),
            )
        glow = glow.filter(ImageFilter.GaussianBlur(max(6, font.size // 14)))
        image.alpha_composite(glow)

        # 2) 太めの黒縁取り＋山吹金の本文。
        draw = ImageDraw.Draw(image)
        draw.text(
            (x, baseline_y),
            text,
            font=font,
            anchor="ms",
            fill=(*color, 255),
            stroke_width=stroke_width,
            stroke_fill=(*CARD_COLOR_INK, 255),
        )
    else:
        draw = ImageDraw.Draw(image)
        shadow_offset = max(2, font.size // 22)
        draw.text(
            (x + shadow_offset, baseline_y + shadow_offset),
            text,
            font=font,
            anchor="ms",
            fill=(0, 0, 0, 150),
        )
        draw.text((x, baseline_y), text, font=font, anchor="ms", fill=(*color, 255))


def build_text_card(card_lines: list[dict]) -> bytes:
    """card_lines を、日常風景ムードの背景に載せたポスター風カードとして描画する。

    Gemini が意味のまとまり（文節）ごとに改行・強調指定した行をそのまま使い、
    ここでは単語途中の再折り返しは行わない。行が余白に収まらない場合のみ、
    行の区切りを保ったまま全体を比例縮小する安全策を取る。
    """
    normal_font_path = _resolve_font_path("normal", CARD_FONT_URLS_NORMAL)
    brush_font_path = _resolve_font_path("brush", CARD_FONT_URLS_BRUSH)
    print(f"[font] 振り(normal)フォント: {normal_font_path}")
    print(f"[font] 落とし(brush)フォント: {brush_font_path}")

    try:
        image = _fetch_photo_background(CARD_WIDTH, CARD_HEIGHT)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[background] 写真背景を断念し、ローカル生成の背景にフォールバックします: {exc}",
            file=sys.stderr,
        )
        image = _make_scenic_background(CARD_WIDTH, CARD_HEIGHT)

    # 中央の文字がくっきり読めるよう、暗いフィルターを全体に重ねてコントラストを確保する。
    dark_overlay = Image.new("RGBA", image.size, (0, 0, 0, CARD_OVERLAY_OPACITY))
    image = Image.alpha_composite(image, dark_overlay)
    # さらに中央〜下部にかけて暗くなるヴィネットを重ね、毛筆文字の可読性を高める。
    image = _add_center_bottom_vignette(image)

    max_block_width = CARD_WIDTH - CARD_TEXT_MARGIN * 2
    max_block_height = CARD_HEIGHT - CARD_TEXT_MARGIN * 2

    scale = 1.0
    while True:
        rendered, block_width, block_height, line_spacing = _layout_card_lines(
            card_lines, normal_font_path, brush_font_path, scale
        )
        if block_width <= max_block_width and block_height <= max_block_height:
            break
        if scale <= CARD_MIN_SCALE:
            break
        scale -= 0.05

    x_center = CARD_WIDTH / 2
    y = (CARD_HEIGHT - block_height) / 2
    for line in rendered:
        baseline_y = y + line["ascent"]
        _draw_card_line(image, line, x_center, baseline_y)
        y += line["ascent"] + line["descent"] + line_spacing

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
