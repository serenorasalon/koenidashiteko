"""
声なき多数派 (@koenidashiteko) 下書き＆風刺画自動生成スクリプト。

Gemini API でペルソナに基づく「本音ぼやきテキスト」と、それを象徴する
風刺画イラストの生成プロンプトを作成し、Imagen で画像を生成する。
生成した画像は images/queue/ に保存してリポジトリにコミット＆プッシュし、
テキストと画像プレビューを載せた GitHub Issue を作成する。
"""

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

TEXT_MODEL = "gemini-2.5-flash"
IMAGE_MODEL = "imagen-3.0-generate-002"

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

image_prompt には、シンプルな線画・フラットデザイン・コミカルなタッチの
1コマ漫画風イラストになるよう、スタイル指定を必ず含めてください
（例: "single-panel satirical comic, simple flat line art, minimal colors,
comical and surreal tone, no text in the image"）。
"""


def build_gemini_client() -> genai.Client:
    api_key = os.environ["GEMINI_API_KEY"]
    return genai.Client(api_key=api_key)


def generate_text_and_prompt(client: genai.Client) -> dict:
    response = client.models.generate_content(
        model=TEXT_MODEL,
        contents=PERSONA_PROMPT,
        config=types.GenerateContentConfig(
            temperature=1.0,
            response_mime_type="application/json",
        ),
    )
    raw = response.text.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    data = json.loads(raw)

    text = data["text"].strip()
    image_prompt = data["image_prompt"].strip()
    if not text or not image_prompt:
        raise ValueError(f"Gemini から空の値が返されました: {data!r}")
    return {"text": text, "image_prompt": image_prompt}


def generate_image(client: genai.Client, image_prompt: str) -> bytes:
    result = client.models.generate_images(
        model=IMAGE_MODEL,
        prompt=image_prompt,
        config=types.GenerateImagesConfig(
            number_of_images=1,
            aspect_ratio="1:1",
        ),
    )
    if not result.generated_images:
        raise RuntimeError("Imagen が画像を返しませんでした")
    return result.generated_images[0].image.image_bytes


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

    image_bytes = generate_image(client, draft["image_prompt"])
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
