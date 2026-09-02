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

MIN_SCORE = float(
    os.environ.get("AI_NOTIFY_MIN_SCORE", "0.78")
)


def create_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name=REGION,
        config=Config(
            signature_version="s3v4",
            s3={
                "addressing_style": "path"
            },
        ),
    )


def download_object(s3, key: str, path: Path):
    response = s3.get_object(
        Bucket=BUCKET,
        Key=key,
    )

    path.write_bytes(
        response["Body"].read()
    )


def table_exists(conn, table_name: str):
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type='table'
          AND name=?
        """,
        (table_name,),
    ).fetchone()

    return row is not None


def read_hotlist_results(db_path: Path):
    conn = sqlite3.connect(
        str(db_path)
    )

    conn.row_factory = sqlite3.Row

    try:
        required = {
            "news_items",
            "platforms",
            "ai_filter_tags",
            "ai_filter_results",
        }

        if not all(
            table_exists(conn, name)
            for name in required
        ):
            return []

        latest_row = conn.execute(
            """
            SELECT MAX(created_at) AS latest
            FROM ai_filter_results
            WHERE status='active'
              AND source_type='hotlist'
            """
        ).fetchone()

        latest = (
            latest_row["latest"]
            if latest_row
            else None
        )

        if not latest:
            return []

        rows = conn.execute(
            """
            SELECT
                n.id,
                n.title,
                n.url,
                p.name AS source_name,
                MAX(r.relevance_score) AS score,
                GROUP_CONCAT(
                    DISTINCT t.tag
                ) AS tags
            FROM ai_filter_results r

            JOIN news_items n
              ON r.news_item_id = n.id
             AND r.source_type = 'hotlist'

            LEFT JOIN platforms p
              ON n.platform_id = p.id

            LEFT JOIN ai_filter_tags t
              ON r.tag_id = t.id

            WHERE r.status='active'
              AND r.source_type='hotlist'
              AND r.created_at = ?

            GROUP BY
                n.id,
                n.title,
                n.url,
                p.name

            HAVING MAX(
                r.relevance_score
            ) >= ?

            ORDER BY score DESC
            """,
            (
                latest,
                MIN_SCORE,
            ),
        ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    finally:
        conn.close()


def read_rss_results(db_path: Path):
    conn = sqlite3.connect(
        str(db_path)
    )

    conn.row_factory = sqlite3.Row

    try:
        required = {
            "rss_items",
            "rss_feeds",
            "ai_filter_tags",
            "ai_filter_results",
        }

        if not all(
            table_exists(conn, name)
            for name in required
        ):
            return []

        latest_row = conn.execute(
            """
            SELECT MAX(created_at) AS latest
            FROM ai_filter_results
            WHERE status='active'
              AND source_type='rss'
            """
        ).fetchone()

        latest = (
            latest_row["latest"]
            if latest_row
            else None
        )

        if not latest:
            return []

        rows = conn.execute(
            """
            SELECT
                n.id,
                n.title,
                n.url,
                f.name AS source_name,
                MAX(r.relevance_score) AS score,
                GROUP_CONCAT(
                    DISTINCT t.tag
                ) AS tags,
                n.published_at

            FROM ai_filter_results r

            JOIN rss_items n
              ON r.news_item_id = n.id
             AND r.source_type = 'rss'

            LEFT JOIN rss_feeds f
              ON n.feed_id = f.id

            LEFT JOIN ai_filter_tags t
              ON r.tag_id = t.id

            WHERE r.status='active'
              AND r.source_type='rss'
              AND r.created_at = ?

            GROUP BY
                n.id,
                n.title,
                n.url,
                f.name,
                n.published_at

            HAVING MAX(
                r.relevance_score
            ) >= ?

            ORDER BY score DESC
            """,
            (
                latest,
                MIN_SCORE,
            ),
        ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    finally:
        conn.close()


def format_item(index, item, kind):
    score = float(
        item.get("score") or 0
    )

    source = (
        item.get("source_name")
        or "Unknown source"
    )

    tags = (
        item.get("tags")
        or ""
    )

    title = (
        item.get("title")
        or "Untitled"
    )

    url = (
        item.get("url")
        or ""
    )

    lines = [
        f"{index}. {title}",
        f"   Source: {source}",
        f"   Score: {score:.2f}",
    ]

    if tags:
        lines.append(
            f"   Tags: {tags}"
        )

    if (
        kind == "rss"
        and item.get("published_at")
    ):
        lines.append(
            "   Published: "
            f"{item['published_at']}"
        )

    if url:
        lines.append(
            f"   URL: {url}"
        )

    return "\n".join(lines)


def build_body(hotlist, rss):
    total = (
        len(hotlist)
        + len(rss)
    )

    if total == 0:
        return ""

    lines = [
        "TrendRadar AI frontier/business brief",
        f"Threshold: {MIN_SCORE:.2f}",
        f"Qualified items: {total}",
        "",
    ]

    index = 1

    if hotlist:
        lines.extend([
            "[Hotlist]",
            "",
        ])

        for item in hotlist:
            lines.append(
                format_item(
                    index,
                    item,
                    "hotlist",
                )
            )

            lines.append("")

            index += 1

    if rss:
        lines.extend([
            "[RSS]",
            "",
        ])

        for item in rss:
            lines.append(
                format_item(
                    index,
                    item,
                    "rss",
                )
            )

            lines.append("")

            index += 1

    return "\n".join(
        lines
    ).strip()


def main():
    s3 = create_s3_client()

    today = datetime.now(
        ZoneInfo("Asia/Shanghai")
    ).strftime("%Y-%m-%d")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        news_db = (
            tmp_path
            / "news.db"
        )

        rss_db = (
            tmp_path
            / "rss.db"
        )

        download_object(
            s3,
            f"news/{today}.db",
            news_db,
        )

        hotlist = (
            read_hotlist_results(
                news_db
            )
        )

        try:
            download_object(
                s3,
                f"rss/{today}.db",
                rss_db,
            )

            rss = (
                read_rss_results(
                    rss_db
                )
            )

        except Exception as exc:
            print(
                "RSS 数据读取失败，"
                "继续使用热榜结果: "
                f"{type(exc).__name__}: {exc}"
            )

            rss = []

    body = build_body(
        hotlist,
        rss,
    )

    if not body:
        print(
            f"没有 score >= "
            f"{MIN_SCORE:.2f} "
            "的新结果，不创建通知"
        )
        return

    now = datetime.now(
        ZoneInfo("Asia/Shanghai")
    )

    payload = {
        "id": now.strftime(
            "%Y%m%d-%H%M%S"
        ),
        "time": now.isoformat(),
        "title":
            "TrendRadar AI 前沿简报",
        "body": body,
    }

    data = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")

    key = (
        f"notify/"
        f"{payload['id']}.json"
    )

    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=data,
        ContentType=(
            "application/json; "
            "charset=utf-8"
        ),
    )

    s3.put_object(
        Bucket=BUCKET,
        Key="notify/latest.json",
        Body=data,
        ContentType=(
            "application/json; "
            "charset=utf-8"
        ),
    )

    print(
        "符合条件结果: "
        f"{len(hotlist) + len(rss)} 条 "
        f"(hotlist={len(hotlist)}, "
        f"rss={len(rss)})"
    )

    print(
        f"通知文件已上传: {key}"
    )

    print(
        "通知最新文件已更新: "
        "notify/latest.json"
    )


if __name__ == "__main__":
    main()
