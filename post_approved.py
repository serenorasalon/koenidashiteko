"""
声なき多数派 (@koenidashiteko) 承認後投稿スクリプト。

`approved` ラベルが付いた Issue を対象に、Issue本文に埋め込まれたメタデータ
（本文テキスト・画像パス）を読み取り、X API で画像付き投稿を行う。
投稿後は画像を images/queue/ から images/posted/ に移動してコミット＆プッシュし、
Issue にリンクを添えてクローズする。
"""

import json
import os
import re
import subprocess
import sys

import requests
import tweepy

META_PATTERN = re.compile(r"<!--KOENIDASHITEKO_META\s*(\{.*?\})\s*-->", re.DOTALL)


def github_headers() -> dict:
    token = os.environ["GITHUB_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }


def get_target_issues() -> list[dict]:
    repo = os.environ["GITHUB_REPOSITORY"]
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    issue_number = os.environ.get("ISSUE_NUMBER")

    if event_name == "issues" and issue_number:
        resp = requests.get(
            f"https://api.github.com/repos/{repo}/issues/{issue_number}",
            headers=github_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return [resp.json()]

    resp = requests.get(
        f"https://api.github.com/repos/{repo}/issues",
        headers=github_headers(),
        params={"state": "open", "labels": "approved", "per_page": 100},
        timeout=30,
    )
    resp.raise_for_status()
    return [issue for issue in resp.json() if "pull_request" not in issue]


def parse_meta(issue: dict) -> dict:
    body = issue.get("body") or ""
    match = META_PATTERN.search(body)
    if not match:
        raise ValueError(f"Issue #{issue['number']} にメタデータが見つかりません")
    return json.loads(match.group(1))


def _log_http_error(label: str, e: "tweepy.errors.HTTPException") -> None:
    """tweepyのHTTPExceptionから可能な限り詳細な情報をActionsログに出力する。"""
    status = getattr(e.response, "status_code", "unknown")
    reason = getattr(e.response, "reason", "")
    body = getattr(e.response, "text", "")

    print(f"::error::{label}に失敗しました: HTTP {status} {reason}")
    print(f"[ERROR] {label}に失敗しました: HTTP {status} {reason}", file=sys.stderr)
    if e.api_codes or e.api_messages:
        print(f"[ERROR]   X APIエラーコード: {e.api_codes} / メッセージ: {e.api_messages}", file=sys.stderr)
    if body:
        print(f"[ERROR]   レスポンス本文: {body[:2000]}", file=sys.stderr)
    print(f"[ERROR]   {e}", file=sys.stderr)

    if status == 403:
        print(
            "[ERROR]   403 Forbiddenは、X Developerアプリの権限が"
            "「Read and Write」になっていない場合や、"
            "契約中のAPIアクセスレベル（Freeプラン等）が"
            "このエンドポイント（media/upload等）を許可していない場合に"
            "発生します。Developer Portalでアプリの権限とアクセスレベルを確認してください。",
            file=sys.stderr,
        )


def upload_media(image_path: str) -> str | None:
    """画像をX (API v1.1 media/upload) にアップロードする。

    失敗した場合はNoneを返し、呼び出し側でテキストのみの投稿にフォールバックできるようにする。
    """
    api_key = os.environ["X_API_KEY"]
    api_secret = os.environ["X_API_SECRET"]
    access_token = os.environ["X_ACCESS_TOKEN"]
    access_token_secret = os.environ["X_ACCESS_TOKEN_SECRET"]

    print(
        f"[DEBUG] media_upload開始: path={image_path} "
        f"abs_path={os.path.abspath(image_path)} "
        f"size={os.path.getsize(image_path)}bytes"
    )

    try:
        auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_token_secret)
        api_v1 = tweepy.API(auth)
        media = api_v1.media_upload(filename=image_path)
        print(f"[DEBUG] media_upload成功: media_id={media.media_id}")
        return str(media.media_id)
    except tweepy.errors.HTTPException as e:
        _log_http_error("画像アップロード(media/upload)", e)
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 画像アップロードで予期しないエラー: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def post_to_x(text: str, image_path: str | None) -> tuple[str, bool]:
    """Xへ投稿する。戻り値は (tweet_url, 画像が実際に添付されたか)。"""
    api_key = os.environ["X_API_KEY"]
    api_secret = os.environ["X_API_SECRET"]
    access_token = os.environ["X_ACCESS_TOKEN"]
    access_token_secret = os.environ["X_ACCESS_TOKEN_SECRET"]

    media_id = None
    if image_path:
        media_id = upload_media(image_path)
        if media_id is None:
            print(
                "[WARN] 画像の添付に失敗したため、テキストのみで投稿を続行します。",
                file=sys.stderr,
            )

    client = tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_token_secret,
    )

    print(f"[DEBUG] create_tweet呼び出し: text_len={len(text)} media_id={media_id}")
    try:
        if media_id:
            result = client.create_tweet(text=text, media_ids=[media_id])
        else:
            result = client.create_tweet(text=text)
    except tweepy.errors.HTTPException as e:
        _log_http_error("ツイート投稿(create_tweet)", e)
        raise

    tweet_id = result.data["id"]
    return f"https://x.com/koenidashiteko/status/{tweet_id}", media_id is not None


def move_image(image_path: str) -> str:
    new_path = image_path.replace("images/queue/", "images/posted/")
    os.makedirs(os.path.dirname(new_path), exist_ok=True)
    subprocess.run(["git", "mv", image_path, new_path], check=True)
    return new_path


def git_commit_and_push(message: str) -> None:
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        check=True,
    )
    status = subprocess.run(["git", "diff", "--staged", "--quiet"])
    if status.returncode == 0:
        print("コミット対象の変更がありません。スキップします。")
        return
    subprocess.run(["git", "commit", "-m", message], check=True)
    subprocess.run(["git", "push"], check=True)


def comment_and_close(issue_number: int, comment: str, add_label: str, remove_label: str) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    headers = github_headers()

    requests.post(
        f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments",
        headers=headers,
        json={"body": comment},
        timeout=30,
    ).raise_for_status()

    requests.delete(
        f"https://api.github.com/repos/{repo}/issues/{issue_number}/labels/{remove_label}",
        headers=headers,
        timeout=30,
    )

    requests.post(
        f"https://api.github.com/repos/{repo}/issues/{issue_number}/labels",
        headers=headers,
        json={"labels": [add_label]},
        timeout=30,
    ).raise_for_status()

    requests.patch(
        f"https://api.github.com/repos/{repo}/issues/{issue_number}",
        headers=headers,
        json={"state": "closed"},
        timeout=30,
    ).raise_for_status()


def process_issue(issue: dict) -> None:
    number = issue["number"]
    print(f"Issue #{number} を処理します")

    try:
        meta = parse_meta(issue)
        text = meta["text"]
        image_path = meta.get("image_path")

        if image_path:
            cwd = os.getcwd()
            abs_path = os.path.abspath(image_path)
            exists = os.path.exists(image_path)
            print(f"[DEBUG] cwd={cwd}")
            print(f"[DEBUG] target_image={image_path} (絶対パス={abs_path}, 存在={exists})")
            if not exists:
                queue_dir = os.path.dirname(image_path) or "."
                if os.path.isdir(queue_dir):
                    print(f"[DEBUG] {queue_dir} の内容: {sorted(os.listdir(queue_dir))}")
                else:
                    print(f"[DEBUG] ディレクトリが存在しません: {queue_dir}")
                raise FileNotFoundError(f"画像が見つかりません: {image_path}")

        tweet_url, media_attached = post_to_x(text, image_path)
        print(f"投稿しました: {tweet_url} (画像添付: {media_attached})")

        if image_path and media_attached:
            new_path = move_image(image_path)
            git_commit_and_push(f"chore: mark posted {os.path.basename(new_path)}")
        elif image_path:
            print(
                f"[WARN] 画像添付に失敗したためテキストのみで投稿されました。"
                f"{image_path} は images/queue/ に残します（再利用の可能性）。"
            )
        else:
            print("画像なしの投稿のため、画像の移動はスキップします。")

        comment = f"投稿が完了しました: {tweet_url}"
        if image_path and not media_attached:
            comment += (
                "\n\n⚠️ 画像の添付に失敗したため、テキストのみで投稿されました。"
                "詳細は GitHub Actions のログを確認してください。"
            )
        comment_and_close(
            number,
            comment,
            add_label="posted",
            remove_label="approved",
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"Issue #{number} の処理中にエラーが発生しました: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        repo = os.environ["GITHUB_REPOSITORY"]
        requests.post(
            f"https://api.github.com/repos/{repo}/issues/{number}/comments",
            headers=github_headers(),
            json={
                "body": (
                    "投稿処理でエラーが発生しました:\n"
                    f"```\n{type(exc).__name__}: {exc}\n```\n\n"
                    "詳細は GitHub Actions のログを確認してください。"
                )
            },
            timeout=30,
        )
        requests.post(
            f"https://api.github.com/repos/{repo}/issues/{number}/labels",
            headers=github_headers(),
            json={"labels": ["post-failed"]},
            timeout=30,
        )


def main() -> None:
    issues = get_target_issues()
    if not issues:
        print("対象の Issue はありません。")
        return
    for issue in issues:
        process_issue(issue)


if __name__ == "__main__":
    main()
