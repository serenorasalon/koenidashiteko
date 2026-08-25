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


def post_to_x(text: str, image_path: str | None) -> str:
    api_key = os.environ["X_API_KEY"]
    api_secret = os.environ["X_API_SECRET"]
    access_token = os.environ["X_ACCESS_TOKEN"]
    access_token_secret = os.environ["X_ACCESS_TOKEN_SECRET"]

    media_ids = None
    if image_path:
        auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_token_secret)
        api_v1 = tweepy.API(auth)
        media = api_v1.media_upload(filename=image_path)
        media_ids = [media.media_id]

    client = tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_token_secret,
    )
    if media_ids:
        result = client.create_tweet(text=text, media_ids=media_ids)
    else:
        result = client.create_tweet(text=text)
    tweet_id = result.data["id"]
    return f"https://x.com/koenidashiteko/status/{tweet_id}"


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

        if image_path and not os.path.exists(image_path):
            raise FileNotFoundError(f"画像が見つかりません: {image_path}")

        tweet_url = post_to_x(text, image_path)
        print(f"投稿しました: {tweet_url}")

        if image_path:
            new_path = move_image(image_path)
            git_commit_and_push(f"chore: mark posted {os.path.basename(new_path)}")
        else:
            print("画像なしの投稿のため、画像の移動はスキップします。")

        comment_and_close(
            number,
            f"投稿が完了しました: {tweet_url}",
            add_label="posted",
            remove_label="approved",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Issue #{number} の処理中にエラーが発生しました: {exc}", file=sys.stderr)
        repo = os.environ["GITHUB_REPOSITORY"]
        requests.post(
            f"https://api.github.com/repos/{repo}/issues/{number}/comments",
            headers=github_headers(),
            json={"body": f"投稿処理でエラーが発生しました:\n```\n{exc}\n```"},
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
