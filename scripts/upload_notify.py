import json
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
from botocore.config import Config

ENDPOINT = os.environ["S3_ENDPOINT_URL"]
BUCKET = os.environ["S3_BUCKET_NAME"]
ACCESS_KEY = os.environ["S3_ACCESS_KEY_ID"]
SECRET_KEY = os.environ["S3_SECRET_ACCESS_KEY"]
REGION = os.environ.get("S3_REGION", "us-east-1")
MIN_SCORE = float(os.environ.get("AI_NOTIFY_MIN_SCORE", "0.72"))


def create_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name=REGION,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )


def download_object(s3, key: str, path: Path):
    response = s3.get_object(Bucket=BUCKET, Key=key)
    path.write_bytes(response["Body"].read())


def table_exists(conn, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def read_ai_results(db_path: Path):
    """Read BOTH hotlist and RSS AI results from the shared news DB."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        required = {"ai_filter_results", "ai_filter_tags", "news_items", "rss_items"}
        if not all(table_exists(conn, name) for name in required):
            missing = [name for name in required if not table_exists(conn, name)]
            print(f"AI result tables missing: {', '.join(missing)}")
            return []

        rows = conn.execute(
            """
            SELECT
                r.news_item_id AS item_id,
                r.source_type,
                MAX(r.relevance_score) AS score,
                GROUP_CONCAT(DISTINCT t.tag) AS tags,
                CASE
                    WHEN r.source_type = 'hotlist' THEN n.title
                    ELSE ri.title
                END AS title,
                CASE
                    WHEN r.source_type = 'hotlist' THEN n.url
                    ELSE ri.url
                END AS url,
                CASE
                    WHEN r.source_type = 'hotlist' THEN p.name
                    ELSE f.name
                END AS source_name,
                ri.published_at
            FROM ai_filter_results r
            LEFT JOIN news_items n
              ON r.source_type = 'hotlist'
             AND r.news_item_id = n.id
            LEFT JOIN platforms p
              ON n.platform_id = p.id
            LEFT JOIN rss_items ri
              ON r.source_type = 'rss'
             AND r.news_item_id = ri.id
            LEFT JOIN rss_feeds f
              ON ri.feed_id = f.id
            LEFT JOIN ai_filter_tags t
              ON r.tag_id = t.id
            WHERE r.status = 'active'
              AND r.source_type IN ('hotlist', 'rss')
            GROUP BY
                r.news_item_id,
                r.source_type,
                title,
                url,
                source_name,
                ri.published_at
            HAVING MAX(r.relevance_score) >= ?
            ORDER BY score DESC
            """,
            (MIN_SCORE,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def dedupe_items(items):
    seen = set()
    result = []
    for item in items:
        key = item.get("url") or (item.get("source_type"), item.get("item_id"), item.get("title"))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def format_item(index, item):
    score = float(item.get("score") or 0)
    source = item.get("source_name") or "Unknown source"
    title = item.get("title") or "Untitled"
    url = item.get("url") or ""
    tags = item.get("tags") or ""
    kind = item.get("source_type", "")

    lines = [
        f"{index}. {title}",
        f"   Source: {source} ({kind})",
        f"   Score: {score:.2f}",
    ]
    if tags:
        lines.append(f"   Tags: {tags}")
    if item.get("published_at"):
        lines.append(f"   Published: {item['published_at']}")
    if url:
        lines.append(f"   URL: {url}")
    return "\n".join(lines)


def build_body(items):
    if not items:
        return ""

    lines = [
        "TrendRadar AI 前沿 / 商业机会简报",
        f"最低评分: {MIN_SCORE:.2f}",
        f"符合条件: {len(items)} 条",
        "",
    ]
    for i, item in enumerate(items, 1):
        lines.append(format_item(i, item))
        lines.append("")
    return "\n".join(lines).strip()


def main():
    s3 = create_s3_client()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "news.db"
        download_object(s3, f"news/{today}.db", db_path)
        items = dedupe_items(read_ai_results(db_path))

    if not items:
        print(f"没有 score >= {MIN_SCORE:.2f} 的结果，不创建通知")
        return

    body = build_body(items)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    payload = {
        "id": now.strftime("%Y%m%d-%H%M%S"),
        "time": now.isoformat(),
        "title": "TrendRadar AI 前沿 / 商业机会简报",
        "body": body,
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    key = f"notify/{payload['id']}.json"

    s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType="application/json; charset=utf-8")
    s3.put_object(Bucket=BUCKET, Key="notify/latest.json", Body=data, ContentType="application/json; charset=utf-8")

    print(f"符合条件结果: {len(items)} 条")
    print(f"通知文件已上传: {key}")
    print("通知最新文件已更新: notify/latest.json")


if __name__ == "__main__":
    main()
