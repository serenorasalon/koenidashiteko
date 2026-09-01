"""
声なき多数派 (@koenidashiteko) 下書き＆画像自動生成スクリプト。

Gemini API で「社会人に響く偉人・アスリートの名言と仕事への活かし方」の
投稿本文と、それに添えるイラスト画像用の英語プロンプトを生成する。画像は
OpenAI の gpt-image-1 で生成し、images/queue/ に保存してリポジトリに
コミット＆プッシュし、テキストと画像プレビューを載せた GitHub Issue を
作成する。
"""

import base64
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

# 最優先で使用するテキスト生成モデル。
PREFERRED_TEXT_MODEL = "gemini-3.6-flash"
# API が動的なモデル一覧取得に失敗した場合にのみ使う最終フォールバック。
# （通常は client.models.list() で取得した現行モデルが優先される）
STATIC_TEXT_MODEL_FALLBACKS = [
    "gemini-3.6-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-2.0-flash-exp",
]
# テキスト生成モデルの一覧から除外する（generateContent はサポートするが
# 用途が異なる）モデル名の断片。
# "deep-research-*" や "antigravity-*" 等の一部プレビューモデルは
# supported_actions に generateContent を含みつつも実際には通常の
# generateContent 呼び出しに対応していない（Interactions API専用等）ため、
# 数値バージョンによる自動ソートだけに頼らず明示的に除外する。
TEXT_MODEL_EXCLUDE = (
    "imagen",
    "embedding",
    "aqa",
    "-image",
    "deep-research",
    "antigravity",
    "lyria",
    "-tts",
    "computer-use",
    "-robotics-",
    "gemma-",
)

# 画像生成モデル（OpenAI）。DALL-E 3 はAPI側で廃止されたため、後継の
# gpt-image-1 を使用する。gpt-image-1 は response_format パラメータを
# 受け付けず、常に b64_json を返す。
OPENAI_IMAGE_MODEL = "gpt-image-1"
OPENAI_IMAGE_SIZE = "1024x1024"
OPENAI_IMAGE_QUALITY = "high"

JST = timezone(timedelta(hours=9))

# 曜日ごとの投稿テーマ（datetime.weekday(): 月=0 ... 日=6）。
# 日本時間（JST）基準で当日の曜日を判定し、Gemini への生成プロンプトに
# その日のテーマを明示的に注入することで、投稿内容を曜日ごとに切り替える。
# コンセプト: 社会人に響く偉人・アスリートの名言と、仕事への活かし方。
WEEKDAY_THEMES = {
    0: "挑戦・スタート（アスリート・冒険家の名言）",
    1: "集中・習慣（職人・ストイックな選手の名言）",
    2: "チームワーク・対話（名監督・リーダーの名言）",
    3: "逆境・失敗からの再起（発明家・偉人の名言）",
    4: "達成・自分を労う言葉（哲学者・表現者の名言）",
    5: "休息・思考の余白（文豪・自然科学者の名言）",
    6: "休息・思考の余白（文豪・自然科学者の名言）",
}


def get_today_theme(now: datetime | None = None) -> str:
    """JST基準の当日の曜日から、本日投稿すべきテーマを返す。"""
    now = now or datetime.now(JST)
    return WEEKDAY_THEMES[now.weekday()]


PERSONA_PROMPT_HEADER = """あなたはX（旧Twitter）アカウント「声なき多数派」(@koenidashiteko) の
中の人です。このアカウントは、日々の激務に追われる社会人に向けて、偉人・
アスリート・リーダーたちの名言と、それを「明日からの仕事にどう活かすか」
を短く深く伝える投稿を1つ作成します。説教くさい自己啓発臭は避け、忙しい
社会人の心に静かに刺さる、洗練された一言にすること。

投稿本文（text）は、以下の2要素をこの順につなげ、1つの自然な文章として
完結させること。
1. 名言そのもの（発言者名を明記すること）。
2. その名言を現代の社会人の仕事・働き方にどう活かせるかを説く、短く
   鋭い解説（長い状況説明やポエム調は禁止。一言一言の言葉の切れ味を
   最優先にすること）。
名言と解説の間には改行を1つ入れ、読み手が一拍置いて意味を噛みしめる
間（ま）を作ること。

あわせて、この投稿に添えるイラスト画像用の英語プロンプト（image_prompt）
も作成すること。画像生成モデルは正確な文字を描画できないため、画像には
文字・テキスト・ロゴ・サイン等を一切含めないこと。名言の世界観・情景を
象徴する、洗練されたシネマティックイラストまたは高品質な3Dアート調の
ビジュアルにすること。
"""


def _build_theme_section(theme: str) -> str:
    """本日のテーマ（曜日別）を明示的に注入するプロンプト断片を作る。"""
    return (
        "# 本日のテーマ（最優先で厳守すること。他の曜日向けの人物像や、"
        "このテーマから外れる題材は選ばないこと）\n"
        f"本日のテーマ: {theme}\n\n"
        "このテーマに沿った人物（偉人・アスリート・リーダー等）の実在する"
        "名言を1つ選び、その名言と、現代の社会人への活かし方を短く伝える"
        "投稿を作成してください。\n"
    )


PERSONA_PROMPT_RULES = """# 投稿のルール
- 名言は実在の人物の発言として広く知られているものを使うこと。発言者名を
  必ず明記すること（例:「〜」――イチロー）。捏造・出典不明な名言や、
  発言者を誤って表記することは禁止。
- 解説部分は説教くささをゼロにし、短く鋭い一言にすること。「わかる」
  「刺さる」「今日から意識したい」と思わせる余韻を残すこと。
- 絵文字やハッシュタグは使わないこと。
- フォロワーへのアンケート・「〜ですよね？」のような質問形式は禁止。
- text は名言＋解説を合わせて60〜100文字程度を目安にし、Xの文字数制限
  内に自然に収まる長さにすること。
- 機械的な文字数折り返しは禁止し、必ず「文節（意味のまとまり）」で改行
  すること。単語や複合語の途中で改行してはいけません。

## image_prompt（画像生成プロンプト）のルール
- 英語で、gpt-image-1向けに具体的かつ簡潔に記述すること。
- 名言の世界観・情景を象徴する、洗練されたシネマティックイラストまたは
  高品質な3Dアートのビジュアルにすること（例: cinematic digital
  illustration, elegant 3D render, dramatic lighting, sophisticated
  color grading）。安っぽいクリップアート調やステレオタイプな「成功哲学
  系」の画像は避けること。
- 文字・テキスト・ロゴ・サイン・タイポグラフィを一切含めないよう必ず
  明示すること（例: "no text, no typography, no logos, no signature,
  no writing of any kind"）。

# 参考例（文体・フォーマットのみの参考。実際の内容は必ず本日のテーマに
沿ったものにすること）
{
  "text": "「準備とは、勝つ前から勝っていることだ」――ラグビー元日本代表HC エディー・ジョーンズ\\n明日の会議の結果は、今夜のあなたの机の上に、もう決まっている。",
  "image_prompt": "A single desk lamp illuminating a neatly organized desk late at night, papers and a notebook laid out with quiet determination, elegant cinematic 3D render, dramatic warm and cool lighting contrast, minimalist and sophisticated mood, no text, no typography, no logos, no signature, no writing of any kind"
}
{
  "text": "「失敗とは、より賢く再挑戦するための機会にすぎない」――発明家 トーマス・エジソン\\n今日のミスは経歴の傷ではなく、次の設計図の下書きだ。",
  "image_prompt": "A cracked lightbulb glowing softly beside a sketchbook full of technical drawings on a wooden workbench, warm golden light, elegant cinematic digital illustration, sense of quiet resilience and craftsmanship, no text, no typography, no logos, no signature, no writing of any kind"
}
{
  "text": "「明日死ぬかのように生きよ。永遠に生きるかのように学べ」――ガンジー\\n定時後の30分を、今日の消化ではなく明日への投資に変えてみる。",
  "image_prompt": "An open book and a cup of tea on a quiet windowsill at dusk, soft cinematic lighting, elegant minimalist 3D render style, contemplative and serene atmosphere, no text, no typography, no logos, no signature, no writing of any kind"
}

# 出力形式
以下のキーのみを持つ JSON オブジェクトを1つだけ出力してください。
説明文やマークダウンのコードフェンスは付けないこと。

{
  "text": "生成した投稿本文（名言＋発言者→改行→現代社会人への解説、日本語）",
  "image_prompt": "生成した画像プロンプト（英語）"
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
    """PREFERRED_TEXT_MODELを必ず最優先で試し、それが失敗した場合のみ
    動的発見・静的フォールバックの順で他モデルにフォールバックする。

    モデル名に含まれる数値（日付やプレビュー版番号等）だけで新しい順に
    ソートすると、"deep-research-pro-preview-12-2025" のような無関係な
    プレビューモデルが本来使いたい安定版より上位に来てしまうことがある
    ため、優先モデルを明示的に固定する。
    """
    discovered = discover_models(client, action="generateContent")
    discovered = [
        name
        for name in discovered
        if not any(excluded in name for excluded in TEXT_MODEL_EXCLUDE)
    ]
    return _dedupe([PREFERRED_TEXT_MODEL] + discovered + STATIC_TEXT_MODEL_FALLBACKS)


def generate_draft_texts(client: genai.Client) -> dict:
    """投稿本文（text）と画像生成プロンプト（image_prompt）を生成する。

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

        text = str(data["text"]).strip()
        image_prompt = str(data["image_prompt"]).strip()
        if not text or not image_prompt:
            raise ValueError(f"Gemini から空の値が返されました: {data!r}")

        print(f"[text] モデル '{model}' でテキストを生成しました。")
        return {"text": text, "image_prompt": image_prompt}

    raise RuntimeError(
        f"すべてのテキスト生成モデル候補 {candidates} で失敗しました"
    ) from last_error


def build_openai_client():
    from openai import OpenAI

    api_key = os.environ["OPENAI_API_KEY"]
    return OpenAI(api_key=api_key)


def generate_image(image_prompt: str) -> bytes:
    """OpenAI gpt-image-1 で画像を生成し、PNGバイト列を返す。"""
    client = build_openai_client()

    try:
        response = client.images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=image_prompt,
            size=OPENAI_IMAGE_SIZE,
            quality=OPENAI_IMAGE_QUALITY,
            n=1,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"OpenAI画像生成に失敗しました: {exc}") from exc

    if not response.data:
        raise RuntimeError("OpenAIから画像データが返されませんでした。")

    b64_data = response.data[0].b64_json
    if not b64_data:
        raise RuntimeError("OpenAIのレスポンスにb64_jsonが含まれていません。")

    print(f"[image] '{OPENAI_IMAGE_MODEL}' で画像を生成しました。")
    return base64.b64decode(b64_data)


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


def create_issue(text: str, image_prompt: str, image_path: str) -> dict:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    branch = os.environ.get("GITHUB_REF_NAME", "main")

    raw_image_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{image_path}"
    # post_approved.py は meta["text"] をそのまま X の投稿本文として使う。
    meta = json.dumps({"text": text, "image_path": image_path}, ensure_ascii=False)

    text_preview = "\n".join(f"> {line}" for line in text.split("\n"))

    body = (
        f"## 投稿テキスト\n\n"
        f"{text_preview}\n\n"
        f"## 画像プロンプト（English, gpt-image-1）\n\n"
        f"> {image_prompt}\n\n"
        f"## 画像プレビュー\n\n"
        f"![draft image]({raw_image_url})\n\n"
        f"---\n"
        f"このIssueに `approved` ラベルを付けると自動投稿されます。\n\n"
        f"<!--KOENIDASHITEKO_META\n{meta}\n-->\n"
    )

    now_jst = datetime.now(JST)
    title_text = text.replace("\n", " ")
    title = f"[下書き] {now_jst.strftime('%Y-%m-%d %H:%M')} JST - {title_text[:20]}"

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
    print(f"投稿本文: {draft['text']}")
    print(f"画像プロンプト: {draft['image_prompt']}")

    image_bytes = generate_image(draft["image_prompt"])
    image_path = save_image(image_bytes)
    print(f"画像を保存しました: {image_path}")
    git_commit_and_push([image_path], f"chore: add draft image {os.path.basename(image_path)}")

    issue = create_issue(draft["text"], draft["image_prompt"], image_path)
    print(f"Issue を作成しました: {issue['html_url']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"エラーが発生しました: {exc}", file=sys.stderr)
        sys.exit(1)
