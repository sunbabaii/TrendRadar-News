import json
import os
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
from botocore.config import Config


ENDPOINT = os.environ["S3_ENDPOINT_URL"]
BUCKET = os.environ["S3_BUCKET_NAME"]
ACCESS_KEY = os.environ["S3_ACCESS_KEY_ID"]
SECRET_KEY = os.environ["S3_SECRET_ACCESS_KEY"]
REGION = os.environ.get("S3_REGION", "us-east-1")


def html_to_text(html: str) -> str:
    from html.parser import HTMLParser

    class Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
            self.in_script = False
            self.in_style = False

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.in_script = True
                if tag == "script":
                    self.in_script = True
                else:
                    self.in_style = True

            if tag in ("br", "p", "div", "section", "article", "h1", "h2", "h3", "li"):
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag == "script":
                self.in_script = False
            elif tag == "style":
                self.in_style = False

        def handle_data(self, data):
            if not self.in_script and not self.in_style:
                text = data.strip()
                if text:
                    self.parts.append(text)

    parser = Parser()
    parser.feed(html)

    lines = []
    for part in parser.parts:
        part = " ".join(part.split())
        if part:
            lines.append(part)

    result = "\n".join(lines)

    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")

    return result.strip()


def main():
    html_path = Path("output/html/latest/incremental.html")

    if not html_path.exists():
        raise SystemExit(f"找不到报告: {html_path}")

    html = html_path.read_text(encoding="utf-8")
    text = html_to_text(html)

    now = datetime.now(ZoneInfo("Asia/Shanghai"))

    payload = {
        "id": now.strftime("%Y%m%d-%H%M%S"),
        "time": now.isoformat(),
        "title": "TrendRadar AI 前沿简报",
        "body": text,
    }

    s3 = boto3.client(
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

    key = f"notify/{payload['id']}.json"

    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )

    s3.put_object(
        Bucket=BUCKET,
        Key="notify/latest.json",
        Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )

    print(f"通知文件已上传: {key}")
    print("通知最新文件已更新: notify/latest.json")


if __name__ == "__main__":
    main()
