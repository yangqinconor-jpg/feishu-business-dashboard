#!/usr/bin/env python3
import argparse
import csv
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta


BASE_URL = "https://open.feishu.cn/open-apis"
DEFAULT_TABLE_ID = "tblrjyozsSHvfBWy"


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
    with urllib.request.urlopen(req, timeout=int(os.getenv("FEISHU_HTTP_TIMEOUT_SECONDS", "60"))) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if result.get("code") != 0:
        raise RuntimeError(f"飞书接口失败：{json.dumps(result, ensure_ascii=False)}")
    return result


def get_token():
    data = request_json(
        "POST",
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        payload={"app_id": require_env("FEISHU_APP_ID"), "app_secret": require_env("FEISHU_APP_SECRET")},
    )
    return data["tenant_access_token"]


def list_fields(token, app_token, table_id):
    data = request_json(
        "GET",
        f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/fields?page_size=100",
        token,
    )
    return data.get("data", {}).get("items", [])


def list_records(token, app_token, table_id):
    records = []
    page_token = None
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        data = request_json(
            "GET",
            f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records?{urllib.parse.urlencode(params)}",
            token,
        )
        payload = data.get("data", {})
        records.extend(payload.get("items", []))
        if not payload.get("has_more"):
            return records
        page_token = payload.get("page_token")


def create_record(token, app_token, table_id, fields):
    request_json(
        "POST",
        f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
        token,
        {"fields": fields},
    )


def update_record(token, app_token, table_id, record_id, fields):
    request_json(
        "PUT",
        f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
        token,
        {"fields": fields},
    )


def delete_record(token, app_token, table_id, record_id):
    request_json(
        "DELETE",
        f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
        token,
    )


def chunks(items, size=500):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def batch_create_records(token, app_token, table_id, records):
    for batch in chunks(records):
        request_json(
            "POST",
            f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
            token,
            {"records": [{"fields": fields} for fields in batch]},
        )


def batch_update_records(token, app_token, table_id, records):
    for batch in chunks(records):
        request_json(
            "POST",
            f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update",
            token,
            {"records": batch},
        )


def batch_delete_records(token, app_token, table_id, record_ids):
    for batch in chunks(record_ids):
        request_json(
            "POST",
            f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete",
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


def parse_mysql_rows(start_date, end_date):
    sql_path = os.path.join(os.path.dirname(__file__), "query_daily_user_orders.sql")
    sql = open(sql_path, "r", encoding="utf-8").read()
    sql = re.sub(r"SET @start_at\s*=\s*'[^']*';", f"SET @start_at = '{start_date} 00:00:00';", sql)
    sql = re.sub(r"SET @end_at\s*=\s*'[^']*';", f"SET @end_at = '{end_date} 00:00:00';", sql)

    env = os.environ.copy()
    env["MYSQL_PWD"] = require_env("MYSQL_PASSWORD")
    client = os.getenv("MYSQL_CLIENT", "mysql")
    command = [
        client,
        "-h", require_env("MYSQL_HOST"),
        "-P", os.getenv("MYSQL_PORT", "3306"),
        "-u", require_env("MYSQL_USER"),
        "--default-character-set=utf8mb4",
        "--batch",
        "--raw",
        require_env("MYSQL_DATABASE"),
    ]
    result = subprocess.run(command, input=sql, env=env, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise RuntimeError(f"MySQL 查询失败：{result.stderr.strip()}")
    rows = list(csv.DictReader(result.stdout.splitlines(), delimiter="\t"))
    if not rows:
        raise RuntimeError("查询没有返回订单数据，请检查日期和 SQL 字段")
    return rows


def date_ms(text):
    if not text or str(text).strip().upper() in {"NULL", "NONE"}:
        return None
    value = text.strip()
    if len(value) == 7:
        value += "-01"
    dt = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S" if len(value) >= 19 else "%Y-%m-%d")
    return int(dt.timestamp() * 1000)


def text_value(value):
    if value is None or str(value).strip().upper() in {"NULL", "NONE"}:
        return ""
    return str(value)


def comparable_value(value, field_type):
    if isinstance(value, list):
        value = "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in value
        )
    if value in (None, "", "NULL"):
        return None
    if field_type == 2:
        return round(float(value), 6)
    if field_type == 5:
        return int(float(value))
    return str(value)


def fields_changed(existing_fields, desired_fields, field_types):
    for name, desired in desired_fields.items():
        if name == "同步时间":
            continue
        if comparable_value(existing_fields.get(name), field_types.get(name)) != comparable_value(
            desired, field_types.get(name)
        ):
            return True
    return False


def build_fields(row, field_types):
    mapping = {
        "订单号": "订单号",
        "用户ID": "用户ID",
        "商品名称": "商品名称",
        "成交金额": "成交金额",
        "下单渠道": "下单渠道",
        "成交类型": "成交类型",
        "下单平台": "下单平台",
        "下单时间": "下单时间",
        "下单所属年月": "下单所属年月",
        "注册时间": "注册时间",
        "首购时间": "首购时间",
        "用户类型": "用户类型",
        "订单类型": "订单类型",
        "距首购天数": "距首购天数",
        "是否退款": "是否退款",
        "是否开课": "是否开课",
        "开课时间": "开课时间",
        "关联APP订单号": "关联APP订单号",
        "第三方订单号": "第三方订单号",
        "同步时间": "同步时间",
    }
    fields = {}
    for source, target in mapping.items():
        if target not in field_types:
            continue
        value = row.get(source, "")
        if field_types[target] == 2:
            if value not in (None, "", "NULL"):
                fields[target] = float(value)
        elif field_types[target] == 5:
            converted = date_ms(value)
            if converted is not None:
                fields[target] = converted
        else:
            fields[target] = text_value(value)
    fields["同步时间"] = int(time.time() * 1000)
    return fields


def sync(rows, start_date, end_date, dry_run=False):
    app_token = require_env("FEISHU_APP_TOKEN")
    table_id = os.getenv("FEISHU_ORDER_DETAIL_TABLE_ID", DEFAULT_TABLE_ID)
    token = get_token()
    field_items = list_fields(token, app_token, table_id)
    field_types = {item["field_name"]: item["type"] for item in field_items}
    required = {"订单号", "用户ID", "商品名称", "成交金额", "下单时间", "订单类型", "是否退款", "是否开课"}
    missing = sorted(required - set(field_types))
    if missing:
        raise RuntimeError(f"订单明细表缺少字段：{', '.join(missing)}")

    records = list_records(token, app_token, table_id)
    existing = {}
    for record in records:
        value = record.get("fields", {}).get("订单号")
        if isinstance(value, list):
            value = "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in value)
        if value:
            existing[str(value)] = record

    created = updated = skipped = 0
    creates = []
    updates = []
    source_order_numbers = {row["订单号"] for row in rows}
    deleted = 0
    deletes = []
    for order_no, record in existing.items():
        fields = record.get("fields", {})
        timestamp = fields.get("下单时间")
        if not isinstance(timestamp, (int, float)):
            continue
        order_date = datetime.fromtimestamp(timestamp / 1000).date()
        if start_date <= order_date < end_date and order_no not in source_order_numbers:
            if dry_run:
                print("DELETE_STALE", json.dumps({"订单号": order_no}, ensure_ascii=False))
            else:
                deletes.append(record["record_id"])
            deleted += 1
    for row in rows:
        order_no = row["订单号"]
        fields = build_fields(row, field_types)
        record = existing.get(order_no)
        changed = not record or fields_changed(record.get("fields", {}), fields, field_types)
        if dry_run:
            action = "CREATE" if not record else ("UPDATE_CHANGED" if changed else "SKIP_UNCHANGED")
            print(action, json.dumps(fields, ensure_ascii=False))
            if record and not changed:
                skipped += 1
            continue
        if record and changed:
            updates.append({"record_id": record["record_id"], "fields": fields})
        elif not record:
            creates.append(fields)
        else:
            skipped += 1
    if not dry_run:
        batch_delete_records(token, app_token, table_id, deletes)
        batch_create_records(token, app_token, table_id, creates)
        batch_update_records(token, app_token, table_id, updates)
        created = len(creates)
        updated = len(updates)
    return created, updated, skipped, deleted


def main():
    parser = argparse.ArgumentParser(description="同步每日订单明细到飞书多维表格")
    parser.add_argument("--env", default="配置文件.env")
    parser.add_argument("--start-date", help="YYYY-MM-DD")
    parser.add_argument("--end-date", help="YYYY-MM-DD，不包含当天")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    load_dotenv(args.env)
    if bool(args.start_date) != bool(args.end_date):
        raise RuntimeError("--start-date 和 --end-date 必须同时使用")
    if args.start_date:
        start_date = parse_date(args.start_date)
        end_date = parse_date(args.end_date)
    else:
        start_date, end_date = resolve_default_range()
    if end_date <= start_date:
        raise RuntimeError("结束日期必须晚于开始日期")
    rows = parse_mysql_rows(start_date.isoformat(), end_date.isoformat())
    created, updated, skipped, deleted = sync(rows, start_date, end_date, dry_run=args.dry_run)
    print(f"订单明细同步完成: created={created}, updated={updated}, skipped={skipped}, deleted={deleted}, total={len(rows)}")


if __name__ == "__main__":
    main()
