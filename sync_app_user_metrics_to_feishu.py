#!/usr/bin/env python3
import argparse
import csv
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

BASE_URL = "https://open.feishu.cn/open-apis"
DEFAULT_TABLE_ID = "tbl5C1L4AiEzFhWj"


def load_dotenv(path):
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def require_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"缺少配置：{name}")
    return value


def request_json(method, url, token=None, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("code") != 0:
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result


def get_token():
    result = request_json(
        "POST",
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        payload={"app_id": require_env("FEISHU_APP_ID"), "app_secret": require_env("FEISHU_APP_SECRET")},
    )
    return result["tenant_access_token"]


def chunks(items, size=500):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def list_records(token, app_token, table_id):
    records = []
    page = None
    while True:
        params = {"page_size": 500}
        if page:
            params["page_token"] = page
        result = request_json(
            "GET",
            f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records?{urllib.parse.urlencode(params)}",
            token,
        )
        data = result.get("data", {})
        records.extend(data.get("items", []))
        if not data.get("has_more"):
            return records
        page = data.get("page_token")


def batch_create(token, app_token, table_id, fields):
    for batch in chunks(fields):
        request_json(
            "POST",
            f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
            token,
            {"records": [{"fields": item} for item in batch]},
        )


def batch_update(token, app_token, table_id, records):
    for batch in chunks(records):
        request_json(
            "POST",
            f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update",
            token,
            {"records": batch},
        )


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def resolve_default_range():
    end_text = os.getenv("SYNC_END_DATE", "").strip()
    end_inclusive = parse_date(end_text) if end_text else date.today()
    start_text = os.getenv("SYNC_START_DATE", "").strip()
    if start_text:
        start_date = parse_date(start_text)
    else:
        lookback_days = int(os.getenv("LOOKBACK_DAYS", "10"))
        if lookback_days < 1:
            raise RuntimeError("LOOKBACK_DAYS 必须大于等于 1")
        start_date = end_inclusive - timedelta(days=lookback_days - 1)
    return start_date, end_inclusive + timedelta(days=1)


def date_ms(value):
    return int(datetime.strptime(value, "%Y-%m-%d").timestamp() * 1000)


def fields_changed(existing_fields, desired_fields):
    return any(
        int(float(existing_fields.get(name, -1))) != int(value)
        for name, value in desired_fields.items()
    )


def next_month(value):
    return date(value.year + (value.month == 12), value.month % 12 + 1, 1)


def query_month_rows(start_date, end_date):
    sql = open(os.path.join(os.path.dirname(__file__), "query_app_user_metrics.sql"), encoding="utf-8").read()
    month_table = f"dyw_data_statistics_pre.event_tracking_{start_date.strftime('%Y%m')}"
    sql = re.sub(r"SET @start_at\s*=\s*'[^']*';", f"SET @start_at = '{start_date.isoformat()} 00:00:00';", sql)
    sql = re.sub(r"SET @end_at\s*=\s*'[^']*';", f"SET @end_at = '{end_date.isoformat()} 00:00:00';", sql)
    sql = sql.replace("FROM @event_table", f"FROM {month_table}")
    env = os.environ.copy()
    env["MYSQL_PWD"] = require_env("MYSQL_PASSWORD")
    command = [
        os.getenv("MYSQL_CLIENT", "/Applications/MySQLWorkbench.app/Contents/MacOS/mysql"),
        "-h", require_env("MYSQL_HOST"), "-P", os.getenv("MYSQL_PORT", "3306"),
        "-u", require_env("MYSQL_USER"), "--default-character-set=utf8mb4", "--batch", "--raw",
        require_env("MYSQL_DATABASE"),
    ]
    result = subprocess.run(command, input=sql, env=env, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return list(csv.DictReader(result.stdout.splitlines(), delimiter="\t"))


def query_rows(start_date, end_date):
    rows = []
    current = start_date
    while current < end_date:
        month_end = min(next_month(current), end_date)
        rows.extend(query_month_rows(current, month_end))
        current = month_end
    return rows


def sync(rows, start_date, end_date, dry_run=False):
    app_token = require_env("FEISHU_APP_TOKEN")
    table_id = os.getenv("FEISHU_APP_USER_DATA_TABLE_ID", DEFAULT_TABLE_ID)
    token = get_token()
    records = list_records(token, app_token, table_id)
    existing = {}
    for record in records:
        value = record.get("fields", {}).get("日期")
        if isinstance(value, (int, float)):
            key = datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d")
            existing[key] = record

    creates, updates = [], []
    skipped = 0
    for row in rows:
        day = row["日期"]
        fields = {
            "日期": date_ms(day),
            "新增注册用户数": int(row["新增注册用户数"]),
            "当日APP活跃人数": int(row["当日APP活跃人数"]),
            "当日APP学习用户人数": int(row["当日APP学习用户人数"]),
        }
        record = existing.get(day)
        changed = not record or fields_changed(record.get("fields", {}), fields)
        if dry_run:
            action = "CREATE" if not record else ("UPDATE_CHANGED" if changed else "SKIP_UNCHANGED")
            print(action, json.dumps(fields, ensure_ascii=False))
            if record and not changed:
                skipped += 1
        elif not record:
            creates.append(fields)
        elif changed:
            updates.append({"record_id": record["record_id"], "fields": fields})
        else:
            skipped += 1
    if not dry_run:
        batch_create(token, app_token, table_id, creates)
        batch_update(token, app_token, table_id, updates)
    return len(creates), len(updates), skipped, len(rows)


def main():
    parser = argparse.ArgumentParser(description="同步每日APP用户数据到飞书")
    parser.add_argument("--env", default="配置文件.env")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date", help="不包含当天")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    load_dotenv(args.env)
    if bool(args.start_date) != bool(args.end_date):
        raise RuntimeError("--start-date 和 --end-date 必须同时使用")
    if args.start_date:
        start_date, end_date = parse_date(args.start_date), parse_date(args.end_date)
    else:
        start_date, end_date = resolve_default_range()
    if end_date <= start_date:
        raise RuntimeError("结束日期必须晚于开始日期")
    rows = query_rows(start_date, end_date)
    created, updated, skipped, total = sync(rows, start_date, end_date, args.dry_run)
    print(f"APP用户数据同步完成: created={created}, updated={updated}, skipped={skipped}, total={total}")


if __name__ == "__main__":
    main()
