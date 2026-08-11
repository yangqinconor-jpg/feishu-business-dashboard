#!/usr/bin/env python3
import argparse
import csv
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta


BASE_URL = "https://open.feishu.cn/open-apis"
DEFAULT_MYSQL_CLIENT = "/Applications/MySQLWorkbench.app/Contents/MacOS/mysql"


def load_dotenv(path):
    if not os.path.exists(path):
        return
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
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid date: {value}, expected YYYY-MM-DD") from exc


def date_range(start_date, end_date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def request_json(method, url, token=None, payload=None):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    timeout_seconds = int(os.getenv("FEISHU_HTTP_TIMEOUT_SECONDS", "60"))
    retries = int(os.getenv("FEISHU_HTTP_RETRIES", "3"))
    last_error = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body}") from e
        except TimeoutError as e:
            last_error = e
        except urllib.error.URLError as e:
            last_error = e
        if attempt < retries:
            time.sleep(attempt * 2)
    raise RuntimeError(f"Feishu API request timed out after {retries} attempt(s): {last_error}")


def assert_feishu_ok(data, action):
    if data.get("code") != 0:
        raise RuntimeError(f"{action} failed: {json.dumps(data, ensure_ascii=False)}")
    return data


def get_tenant_access_token(app_id, app_secret):
    data = request_json(
        "POST",
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        payload={"app_id": app_id, "app_secret": app_secret},
    )
    assert_feishu_ok(data, "get tenant_access_token")
    return data["tenant_access_token"]


def list_fields(token, app_token, table_id):
    data = request_json(
        "GET",
        f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
        token=token,
    )
    assert_feishu_ok(data, "list fields")
    return data.get("data", {}).get("items", [])


def field_type_map(fields):
    return {field.get("field_name"): field.get("type") for field in fields}


def list_records(token, app_token, table_id):
    records = []
    page_token = None
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        url = (
            f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records?"
            + urllib.parse.urlencode(params)
        )
        data = request_json("GET", url, token=token)
        assert_feishu_ok(data, "list records")
        payload = data.get("data", {})
        records.extend(payload.get("items", []))
        if not payload.get("has_more"):
            return records
        page_token = payload.get("page_token")


def create_record(token, app_token, table_id, fields):
    data = request_json(
        "POST",
        f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
        token=token,
        payload={"fields": fields},
    )
    assert_feishu_ok(data, "create record")
    return data.get("data", {}).get("record", {})


def update_record(token, app_token, table_id, record_id, fields):
    data = request_json(
        "PUT",
        f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
        token=token,
        payload={"fields": fields},
    )
    assert_feishu_ok(data, "update record")
    return data.get("data", {}).get("record", {})


def delete_record(token, app_token, table_id, record_id):
    data = request_json(
        "DELETE",
        f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
        token=token,
    )
    assert_feishu_ok(data, "delete record")


def normalize_feishu_text(value):
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("name") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    if value is None:
        return ""
    return str(value)


def normalize_feishu_date(value):
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d")
    text = normalize_feishu_text(value)
    if text.isdigit() and len(text) >= 12:
        return datetime.fromtimestamp(int(text) / 1000).strftime("%Y-%m-%d")
    return text[:10]


def mysql_query_rows(start_date, end_date):
    mysql_client = os.getenv("MYSQL_CLIENT", DEFAULT_MYSQL_CLIENT)
    mysql_host = require_env("MYSQL_HOST")
    mysql_port = os.getenv("MYSQL_PORT", "3306")
    mysql_user = require_env("MYSQL_USER")
    mysql_password = require_env("MYSQL_PASSWORD")
    mysql_database = require_env("MYSQL_DATABASE")
    next_day = end_date + timedelta(days=1)
    video_talent_condition = build_video_talent_condition()

    sql = f"""
SELECT 日期, channel, sale_type, ROUND(amount, 2) AS amount, ROUND(net_amount, 2) AS net_amount, order_count, refund_count
FROM (
    SELECT DATE(create_time) AS 日期, 'APP' AS channel, '自营' AS sale_type, IFNULL(SUM(CASE WHEN order_status = '1' AND pay_type IN (1,2,3) THEN price ELSE 0 END), 0) AS amount, SUM(CASE WHEN order_status = '1' AND pay_type IN (1,2,3) THEN 1 ELSE 0 END) AS order_count, 0 AS refund_count, IFNULL(SUM(CASE WHEN order_status = '1' AND pay_type IN (1,2,3) THEN price ELSE 0 END), 0) AS net_amount, 1 AS sort_order
    FROM user_order
    WHERE create_time >= '{start_date}' AND create_time < '{next_day}'
    GROUP BY DATE(create_time)

    UNION ALL

    SELECT
        DATE(o.create_time) AS 日期,
        '抖音' AS channel,
        CASE
            WHEN o.shop_id = 60582548 THEN '迈科'
            WHEN CAST(IFNULL(o.author_id, '') AS CHAR) IN ('1019954776506510', '299496529994004') THEN '自营'
            WHEN TRIM(IFNULL(o.author_name, '')) IN ('申怡读书', '申怡伴学', '申怡办学') THEN '自营'
            WHEN w.first_time IS NOT NULL AND o.create_time >= w.first_time AND o.create_time <= w.last_time THEN '达人'
            ELSE '自营'
        END AS sale_type,
        IFNULL(SUM(o.price), 0) AS amount,
        COUNT(*) AS order_count,
        SUM(CASE WHEN o.order_status = 4 THEN 1 ELSE 0 END) AS refund_count,
        IFNULL(SUM(CASE WHEN o.order_status = 4 THEN 0 ELSE o.price END), 0) AS net_amount,
        2 AS sort_order
    FROM user_order_sync o
    CROSS JOIN (
        SELECT MIN(create_time) AS first_time, MAX(create_time) AS last_time
        FROM user_order_sync
        WHERE create_time >= '{start_date}' AND create_time < '{next_day}'
          AND shop_id <> 60582548
          AND IFNULL(author_id, '') NOT IN ('', '0')
          AND CAST(IFNULL(author_id, '') AS CHAR) NOT IN ('1019954776506510', '299496529994004')
          AND TRIM(IFNULL(author_name, '')) NOT IN ('申怡读书', '申怡伴学', '申怡办学')
    ) w
    WHERE o.create_time >= '{start_date}' AND o.create_time < '{next_day}'
    GROUP BY DATE(o.create_time), sale_type

    UNION ALL

    SELECT DATE(create_time) AS 日期, '视频号' AS channel, CASE WHEN product_title LIKE '申怡甄选%' THEN '迈科' ELSE {video_talent_condition} END AS sale_type, IFNULL(SUM(order_price / 100), 0) AS amount, COUNT(*) AS order_count, SUM(CASE WHEN status = '200' THEN 1 ELSE 0 END) AS refund_count, IFNULL(SUM(CASE WHEN status = '200' THEN 0 ELSE order_price / 100 END), 0) AS net_amount, 3 AS sort_order
    FROM wx_order
    WHERE create_time >= '{start_date}' AND create_time < '{next_day}'
    GROUP BY DATE(create_time), sale_type

    UNION ALL

    SELECT
        DATE(o.create_time) AS 日期,
        '小红书' AS channel,
        CASE
            WHEN TRIM(IFNULL(o.author_name, '')) = '申怡读书' THEN '自营'
            WHEN w.first_time IS NOT NULL AND o.create_time >= w.first_time AND o.create_time <= w.last_time THEN '达人'
            ELSE '自营'
        END AS sale_type,
        IFNULL(SUM(o.price / 100), 0) AS amount,
        COUNT(*) AS order_count,
        SUM(CASE WHEN o.order_status = 9 THEN 1 ELSE 0 END) AS refund_count,
        IFNULL(SUM(CASE WHEN o.order_status = 9 THEN 0 ELSE o.price / 100 END), 0) AS net_amount,
        4 AS sort_order
    FROM xhs_order o
    CROSS JOIN (
        SELECT MIN(create_time) AS first_time, MAX(create_time) AS last_time
        FROM xhs_order
        WHERE create_time >= '{start_date}' AND create_time < '{next_day}'
          AND IFNULL(author_id, '') NOT IN ('', '0')
          AND TRIM(IFNULL(author_name, '')) <> '申怡读书'
    ) w
    WHERE o.create_time >= '{start_date}' AND o.create_time < '{next_day}'
    GROUP BY DATE(o.create_time), sale_type

    UNION ALL

    SELECT DATE(create_time) AS 日期, '小鹅通私域' AS channel, '自营' AS sale_type, IFNULL(SUM(price / 100), 0) AS amount, COUNT(*) AS order_count, SUM(CASE WHEN order_status = 99 THEN 1 ELSE 0 END) AS refund_count, IFNULL(SUM(CASE WHEN order_status = 99 THEN 0 ELSE price / 100 END), 0) AS net_amount, 5 AS sort_order
    FROM xiaoe_order
    WHERE create_time >= '{start_date}' AND create_time < '{next_day}'
    GROUP BY DATE(create_time)
) x
ORDER BY 日期, sort_order, sale_type;
""".strip()

    env = os.environ.copy()
    env["MYSQL_PWD"] = mysql_password
    cmd = [
        mysql_client,
        "-h",
        mysql_host,
        "-P",
        str(mysql_port),
        "-u",
        mysql_user,
        "--default-character-set=utf8mb4",
        "--batch",
        "--raw",
        mysql_database,
        "-e",
        sql,
    ]
    timeout_seconds = int(os.getenv("MYSQL_QUERY_TIMEOUT_SECONDS", "900"))
    try:
        result = subprocess.run(
            cmd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"MySQL query timed out after {timeout_seconds} seconds") from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    reader = csv.DictReader(result.stdout.splitlines(), delimiter="\t")
    raw_rows = list(reader)
    by_key = {(row["日期"], row["channel"], row["sale_type"]): row for row in raw_rows}

    rows = []
    channel_types = [
        ("APP", ["自营"]),
        ("抖音", ["自营", "达人", "迈科"]),
        ("视频号", ["自营", "达人", "迈科"]),
        ("小红书", ["自营", "达人"]),
        ("小鹅通私域", ["自营"]),
    ]
    for dt in date_range(start_date, end_date):
        day = dt.isoformat()
        for channel, sale_types in channel_types:
            for sale_type in sale_types:
                raw = by_key.get((day, channel, sale_type))
                amount = float(raw["amount"]) if raw else 0.0
                net_amount = float(raw["net_amount"]) if raw else 0.0
                order_count = int(raw["order_count"]) if raw else 0
                refund_count = int(raw["refund_count"]) if raw else 0
                rows.append(
                    {
                        "日期": day,
                        "渠道": channel,
                        "成交类型": sale_type,
                        "金额": round(amount, 2),
                        "退款后金额": round(net_amount, 2),
                        "订单数": order_count,
                        "退款订单数": refund_count,
                    }
                )
    return rows


def build_video_talent_condition():
    windows = os.getenv("VIDEO_TALENT_WINDOWS", "").strip()
    clauses = []
    if windows:
        for item in windows.split(";"):
            item = item.strip()
            if not item:
                continue
            if "|" not in item:
                raise RuntimeError("VIDEO_TALENT_WINDOWS format should be: start|end;start|end")
            start_at, end_at = [part.strip() for part in item.split("|", 1)]
            clauses.append(f"(create_time >= '{start_at}' AND create_time <= '{end_at}')")
    if not clauses:
        return "'自营'"
    return "CASE WHEN " + " OR ".join(clauses) + " THEN '达人' ELSE '自营' END"


def format_value_for_field(field_types, field_name, value):
    field_type = field_types.get(field_name)
    if field_type == 2:
        return float(value)
    if field_type == 5:
        if isinstance(value, int):
            return value
        if isinstance(value, date):
            dt = datetime(value.year, value.month, value.day)
        else:
            dt = datetime.strptime(str(value), "%Y-%m-%d")
        return int(dt.timestamp() * 1000)
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def row_to_feishu_fields(row, field_types, include_identity=True, include_manual_default=False):
    mapping = {
        "日期": os.getenv("FEISHU_FIELD_DATE", "日期"),
        "渠道": os.getenv("FEISHU_FIELD_CHANNEL", "渠道"),
        "成交类型": os.getenv("FEISHU_FIELD_SALE_TYPE", "成交类型"),
        "金额": os.getenv("FEISHU_FIELD_AMOUNT", "金额"),
        "退款后金额": os.getenv("FEISHU_FIELD_NET_AMOUNT", "退款后金额"),
        "订单数": os.getenv("FEISHU_FIELD_ORDER_COUNT", "订单数"),
        "退款订单数": os.getenv("FEISHU_FIELD_REFUND_COUNT", "退款订单数"),
        "同步时间": os.getenv("FEISHU_FIELD_SYNC_TIME", "同步时间"),
        "手动调整": os.getenv("FEISHU_FIELD_MANUAL_ADJUST", "手动调整"),
    }
    now_ms = int(time.time() * 1000)
    fields = {}
    source_keys = ["金额", "退款后金额", "订单数", "退款订单数"]
    if include_identity:
        source_keys = ["日期", "渠道", "成交类型"] + source_keys
    for source_key in source_keys:
        target_key = mapping[source_key]
        if target_key in field_types:
            fields[target_key] = format_value_for_field(field_types, target_key, row[source_key])
    if include_manual_default and mapping["手动调整"] in field_types:
        fields[mapping["手动调整"]] = "否"
    if mapping["同步时间"] in field_types:
        fields[mapping["同步时间"]] = now_ms
    return fields


def parse_feishu_number(value):
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = normalize_feishu_text(value).replace(",", "").strip()
    if not text:
        return 0.0
    return float(text)


def existing_record_index(records, field_date, field_channel, field_sale_type):
    index = {}
    for record in records:
        fields = record.get("fields", {})
        record_id = record.get("record_id")
        dt = normalize_feishu_date(fields.get(field_date))
        channel = normalize_feishu_text(fields.get(field_channel))
        sale_type = normalize_feishu_text(fields.get(field_sale_type))
        if dt and channel and sale_type and record_id:
            index[(dt, channel, sale_type)] = {"record_id": record_id, "fields": fields}
    return index


def manual_adjust_value(value):
    text = normalize_feishu_text(value).strip().lower()
    return value is True or text in {"是", "yes", "true", "1", "y"}


def needs_manual_default(existing_fields, field_manual_adjust):
    if not field_manual_adjust:
        return False
    value = existing_fields.get(field_manual_adjust)
    if manual_adjust_value(value):
        return False
    return normalize_feishu_text(value).strip() != "否"


def same_metric_values(row, existing_fields, field_names):
    comparisons = [
        ("金额", 2),
        ("退款后金额", 2),
        ("订单数", 0),
        ("退款订单数", 0),
    ]
    for source_key, digits in comparisons:
        field_name = field_names.get(source_key)
        if not field_name:
            continue
        current = parse_feishu_number(existing_fields.get(field_name))
        desired = float(row[source_key])
        if round(current, digits) != round(desired, digits):
            return False
    return True


def manual_adjusted_records_by_date_channel(records, field_date, field_channel, field_manual_adjust):
    index = {}
    if not field_manual_adjust:
        return index
    for record in records:
        fields = record.get("fields", {})
        if not manual_adjust_value(fields.get(field_manual_adjust)):
            continue
        dt = normalize_feishu_date(fields.get(field_date))
        channel = normalize_feishu_text(fields.get(field_channel))
        if dt and channel:
            index.setdefault((dt, channel), []).append(record)
    return index


def select_manual_adjusted_record(row, candidates, field_names, used_record_ids):
    available = [
        record
        for record in candidates
        if record.get("record_id") and record.get("record_id") not in used_record_ids
    ]
    if not available:
        return None, "none"
    metric_matches = [
        record
        for record in available
        if same_metric_values(row, record.get("fields", {}), field_names)
    ]
    if len(metric_matches) == 1:
        return metric_matches[0], "matched"
    if len(available) == 1:
        return available[0], "single"
    return None, "ambiguous"


def row_changed(row, existing_fields, field_names):
    comparisons = [
        ("金额", 2),
        ("退款后金额", 2),
        ("订单数", 0),
        ("退款订单数", 0),
    ]
    for source_key, digits in comparisons:
        field_name = field_names.get(source_key)
        if not field_name:
            continue
        current = parse_feishu_number(existing_fields.get(field_name))
        desired = float(row[source_key])
        if round(current, digits) != round(desired, digits):
            return True
    return False


def delete_total_channel_records(token, app_token, table_id, records, field_channel, dry_run=False):
    deleted = 0
    for record in records:
        record_id = record.get("record_id")
        channel = normalize_feishu_text(record.get("fields", {}).get(field_channel))
        if record_id and channel == "全渠道合计":
            if dry_run:
                print("DELETE", json.dumps({"record_id": record_id, "渠道": channel}, ensure_ascii=False))
            else:
                delete_record(token, app_token, table_id, record_id)
            deleted += 1
    return deleted


def delete_untyped_records(token, app_token, table_id, records, field_date, field_channel, field_sale_type, dry_run=False):
    deleted = 0
    for record in records:
        fields = record.get("fields", {})
        record_id = record.get("record_id")
        dt = normalize_feishu_date(fields.get(field_date))
        channel = normalize_feishu_text(fields.get(field_channel))
        sale_type = normalize_feishu_text(fields.get(field_sale_type))
        if record_id and dt and channel and not sale_type:
            if dry_run:
                print("DELETE", json.dumps({"record_id": record_id, "日期": dt, "渠道": channel, "原因": "缺少成交类型"}, ensure_ascii=False))
            else:
                delete_record(token, app_token, table_id, record_id)
            deleted += 1
    return deleted


def sync_rows_to_feishu(rows, update_dates=None, dry_run=False):
    app_id = require_env("FEISHU_APP_ID")
    app_secret = require_env("FEISHU_APP_SECRET")
    app_token = require_env("FEISHU_APP_TOKEN")
    table_id = require_env("FEISHU_TABLE_ID")

    token = get_tenant_access_token(app_id, app_secret)
    fields = list_fields(token, app_token, table_id)
    field_types = field_type_map(fields)

    field_date = os.getenv("FEISHU_FIELD_DATE", "日期")
    field_channel = os.getenv("FEISHU_FIELD_CHANNEL", "渠道")
    field_sale_type = os.getenv("FEISHU_FIELD_SALE_TYPE", "成交类型")
    field_manual_adjust = os.getenv("FEISHU_FIELD_MANUAL_ADJUST", "手动调整")
    field_names = {
        "金额": os.getenv("FEISHU_FIELD_AMOUNT", "金额"),
        "退款后金额": os.getenv("FEISHU_FIELD_NET_AMOUNT", "退款后金额"),
        "订单数": os.getenv("FEISHU_FIELD_ORDER_COUNT", "订单数"),
        "退款订单数": os.getenv("FEISHU_FIELD_REFUND_COUNT", "退款订单数"),
    }
    if field_sale_type not in field_types:
        raise RuntimeError(f"飞书表缺少字段：{field_sale_type}。请新增字段后再运行。")
    if field_manual_adjust not in field_types:
        field_manual_adjust = None
    records = list_records(token, app_token, table_id)
    deleted = 0
    if os.getenv("DELETE_TOTAL_CHANNEL", "true").lower() in {"1", "true", "yes", "y"}:
        deleted = delete_total_channel_records(token, app_token, table_id, records, field_channel, dry_run=dry_run)
        if deleted and not dry_run:
            records = [record for record in records if normalize_feishu_text(record.get("fields", {}).get(field_channel)) != "全渠道合计"]
    if os.getenv("DELETE_UNTYPED_RECORDS", "true").lower() in {"1", "true", "yes", "y"}:
        untyped_deleted = delete_untyped_records(token, app_token, table_id, records, field_date, field_channel, field_sale_type, dry_run=dry_run)
        deleted += untyped_deleted
        if untyped_deleted and not dry_run:
            records = [record for record in records if normalize_feishu_text(record.get("fields", {}).get(field_sale_type))]
    existing = existing_record_index(records, field_date, field_channel, field_sale_type)
    manual_by_date_channel = manual_adjusted_records_by_date_channel(records, field_date, field_channel, field_manual_adjust)

    update_dates = {dt.isoformat() if isinstance(dt, date) else str(dt) for dt in (update_dates or [])}
    created = 0
    updated = 0
    skipped = 0
    used_manual_record_ids = set()
    for row in rows:
        create_fields = row_to_feishu_fields(row, field_types, include_identity=True, include_manual_default=True)
        metric_update_fields = row_to_feishu_fields(row, field_types, include_identity=False, include_manual_default=False)
        normal_update_fields = row_to_feishu_fields(row, field_types, include_identity=False, include_manual_default=True)
        key = (row["日期"], row["渠道"], row["成交类型"])
        existing_entry = existing.get(key)
        record_id = existing_entry["record_id"] if existing_entry else None
        exact_manual_adjusted = bool(
            field_manual_adjust
            and existing_entry
            and manual_adjust_value(existing_entry["fields"].get(field_manual_adjust))
        )
        manual_entry = None
        manual_match_state = "none"
        if field_manual_adjust and not existing_entry:
            manual_entry, manual_match_state = select_manual_adjusted_record(
                row,
                manual_by_date_channel.get((row["日期"], row["渠道"]), []),
                field_names,
                used_manual_record_ids,
            )
            if manual_entry:
                record_id = manual_entry["record_id"]
                used_manual_record_ids.add(record_id)
        should_update = bool(
            record_id
            and (
                row_changed(row, (manual_entry or existing_entry)["fields"], field_names)
                or (
                    not exact_manual_adjusted
                    and not manual_entry
                    and needs_manual_default(existing_entry["fields"], field_manual_adjust)
                )
            )
        )
        update_fields = metric_update_fields if (exact_manual_adjusted or manual_entry) else normal_update_fields
        if dry_run:
            if manual_match_state == "ambiguous":
                action = "SKIP_MANUAL_AMBIGUOUS"
            elif should_update and (exact_manual_adjusted or manual_entry):
                action = "UPDATE_MANUAL_METRICS"
            elif should_update:
                action = "UPDATE_CHANGED"
            elif record_id:
                action = "SKIP_UNCHANGED"
            else:
                action = "CREATE"
            fields_for_print = update_fields if record_id else create_fields
            print(action, json.dumps(fields_for_print, ensure_ascii=False))
            continue
        if manual_match_state == "ambiguous":
            skipped += 1
            continue
        if should_update:
            update_record(token, app_token, table_id, record_id, update_fields)
            updated += 1
        elif not record_id:
            create_record(token, app_token, table_id, create_fields)
            created += 1
        else:
            skipped += 1
    return created, updated, skipped, deleted


def main():
    parser = argparse.ArgumentParser(description="Sync MySQL channel daily report to Feishu Bitable.")
    parser.add_argument("--env", default="work/.env", help="Path to env file. Default: work/.env")
    parser.add_argument("--date", type=parse_date, help="Sync one date, YYYY-MM-DD. Default: yesterday.")
    parser.add_argument("--start-date", type=parse_date, help="Start date, YYYY-MM-DD.")
    parser.add_argument("--end-date", type=parse_date, help="End date, YYYY-MM-DD.")
    parser.add_argument(
        "--check-missing-from",
        type=parse_date,
        help="Check missing rows from this date to the target end date. Existing older rows are skipped.",
    )
    parser.add_argument(
        "--update-all",
        action="store_true",
        help="Update existing rows for the whole queried range instead of only the target date.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print rows without writing to Feishu.")
    args = parser.parse_args()

    load_dotenv(args.env)

    if args.date:
        start_date = args.date
        end_date = args.date
        update_dates = {args.date}
    elif args.start_date or args.end_date:
        if not args.start_date or not args.end_date:
            raise RuntimeError("--start-date and --end-date must be used together")
        start_date = args.start_date
        end_date = args.end_date
        update_dates = set(date_range(start_date, end_date))
    else:
        end_date_text = os.getenv("SYNC_END_DATE", "").strip()
        start_date_text = os.getenv("SYNC_START_DATE", "").strip()
        end_date = parse_date(end_date_text) if end_date_text else date.today()
        if start_date_text:
            start_date = parse_date(start_date_text)
        else:
            lookback_days = int(os.getenv("LOOKBACK_DAYS", "10"))
            if lookback_days < 1:
                raise RuntimeError("LOOKBACK_DAYS must be >= 1")
            start_date = end_date - timedelta(days=lookback_days - 1)
        update_dates = set(date_range(start_date, end_date))

    check_missing_from = args.check_missing_from
    if check_missing_from:
        start_date = min(start_date, check_missing_from)

    if end_date < start_date:
        raise RuntimeError("--end-date cannot be earlier than --start-date")
    if args.update_all:
        update_dates = set(date_range(start_date, end_date))

    rows = mysql_query_rows(start_date, end_date)
    created, updated, skipped, deleted = sync_rows_to_feishu(rows, update_dates=update_dates, dry_run=args.dry_run)
    if args.dry_run:
        print(f"dry-run rows: {len(rows)}")
    else:
        print(f"同步完成: created={created}, updated={updated}, skipped={skipped}, deleted={deleted}, total={len(rows)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
