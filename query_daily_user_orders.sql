-- MySQL 5.7，只读查询。
-- 一行代表一笔平台订单；第三方订单未关联 user_order 时也会保留。
-- 首购定义：用户历史第一笔 user_order.order_status=1 且 price>70 的有效订单。
-- 用户类型：有开课关联时按注册时间判断新/老用户；没有开课关联时为未开课用户。
-- 订单类型：未开课订单、体验课订单、首购、首购当日追加购买、首购7日内追加购买、复购。
-- 是否退款单独判断，不写入订单类型。
-- 修改下面两个时间即可查询不同日期，使用左闭右开区间。
SET @start_at = '2026-08-12 00:00:00';
SET @end_at   = '2026-08-13 00:00:00';

SELECT
    o.order_no AS `订单号`,
    COALESCE(o.source_user_no, um.user_no) AS `用户ID`,
    o.product_name AS `商品名称`,
    ROUND(o.amount, 2) AS `成交金额`,
    o.order_channel AS `下单渠道`,
    o.sale_type AS `成交类型`,
    o.platform AS `下单平台`,
    o.order_time AS `下单时间`,
    DATE_FORMAT(o.order_time, '%Y-%m') AS `下单所属年月`,
    ui.create_time AS `注册时间`,
    CASE
        WHEN COALESCE(o.app_order_no, um.app_order_no) = fp.first_app_order_no THEN o.order_time
        ELSE fp.first_purchase_time
    END AS `首购时间`,
    CASE
        WHEN COALESCE(o.source_user_no, um.user_no) IS NULL THEN '未开课用户'
        WHEN o.order_time < ui.create_time THEN '新用户'
        WHEN o.order_time <= DATE_ADD(ui.create_time, INTERVAL 7 DAY) THEN '新用户'
        ELSE '老用户'
    END AS `用户类型`,
    CASE
        WHEN COALESCE(o.source_user_no, um.user_no) IS NULL THEN '未开课订单'
        WHEN o.amount <= 70 THEN '体验课订单'
        WHEN fp.first_purchase_time IS NULL THEN '体验课订单'
        WHEN COALESCE(o.app_order_no, um.app_order_no) = fp.first_app_order_no THEN '首购'
        WHEN o.order_time < fp.first_purchase_time THEN '体验课订单'
        WHEN DATE(o.order_time) = DATE(fp.first_purchase_time) THEN '首购当日追加购买'
        WHEN DATEDIFF(DATE(o.order_time), DATE(fp.first_purchase_time)) BETWEEN 1 AND 7
            THEN '首购7日内追加购买'
        ELSE '复购'
    END AS `订单类型`,
    CASE
        WHEN COALESCE(o.app_order_no, um.app_order_no) = fp.first_app_order_no THEN 0
        ELSE DATEDIFF(DATE(o.order_time), DATE(fp.first_purchase_time))
    END AS `距首购天数`,
    IF(o.is_refund = 1, '是', '否') AS `是否退款`,
    IF(COALESCE(o.source_user_no, um.user_no) IS NULL, '否', '是') AS `是否开课`,
    CASE
        WHEN o.platform = 'APP' THEN o.order_time
        ELSE um.open_time
    END AS `开课时间`,
    COALESCE(o.app_order_no, um.app_order_no) AS `关联APP订单号`,
    CASE WHEN o.platform = 'APP' THEN NULL ELSE o.order_no END AS `第三方订单号`
FROM (
    /* APP订单：只取没有第三方订单号的 APP 自营订单，避免重复计算平台订单。 */
    SELECT
        u.order_no AS order_no,
        u.order_no AS app_order_no,
        u.user_no AS source_user_no,
        u.business_name AS product_name,
        u.price AS amount,
        '申怡读书' AS order_channel,
        '自营' AS sale_type,
        'APP' AS platform,
        COALESCE(u.pay_time, u.create_time) AS order_time,
        0 AS is_refund
    FROM user_order u
    WHERE (u.third_party_order_no IS NULL OR u.third_party_order_no = '')
      AND u.order_status = 1
      AND u.pay_type IN (1, 2, 3)
      AND COALESCE(u.pay_time, u.create_time) >= @start_at
      AND COALESCE(u.pay_time, u.create_time) < @end_at
      AND u.price > 0

    UNION ALL

    /* 抖音：按平台订单号聚合，避免一个订单多个开课商品重复出现。 */
    SELECT
        d.order_no,
        NULL AS app_order_no,
        NULL AS source_user_no,
        GROUP_CONCAT(DISTINCT d.business_name ORDER BY d.business_name SEPARATOR ' + ') AS product_name,
        MAX(d.price) AS amount,
        COALESCE(NULLIF(TRIM(MAX(d.author_name)), ''), '申怡读书') AS order_channel,
        CASE
            WHEN MAX(d.shop_id) = 60582548
             AND CAST(IFNULL(MAX(d.author_id), '') AS CHAR) = '1019954776506510' THEN '迈科'
            WHEN MAX(d.shop_id) = 60582548 THEN '自营'
            WHEN CAST(IFNULL(MAX(d.author_id), '') AS CHAR) IN ('1019954776506510', '299496529994004') THEN '自营'
            WHEN TRIM(IFNULL(MAX(d.author_name), '')) IN ('申怡读书', '申怡伴学', '申怡办学') THEN '自营'
            WHEN IFNULL(MAX(d.author_id), '') NOT IN ('', '0') THEN '达人'
            ELSE '自营'
        END AS sale_type,
        '抖音' AS platform,
        MIN(COALESCE(d.order_create_time, d.pay_time, d.create_time)) AS order_time,
        IF(MAX(d.order_status = 4), 1, 0) AS is_refund
    FROM user_order_sync d
    WHERE COALESCE(d.order_create_time, d.pay_time, d.create_time) >= @start_at
      AND COALESCE(d.order_create_time, d.pay_time, d.create_time) < @end_at
    GROUP BY d.order_no

    UNION ALL

    /* 视频号：按 order_id 聚合；现有 wx_order 没有达人字段。 */
    SELECT
        w.order_id,
        NULL AS app_order_no,
        NULL AS source_user_no,
        GROUP_CONCAT(DISTINCT w.product_title ORDER BY w.product_title SEPARATOR ' + ') AS product_name,
        MAX(CAST(w.order_price AS DECIMAL(12,2))) / 100 AS amount,
        CASE WHEN MAX(w.product_title) LIKE '申怡甄选%' THEN '申怡甄选' ELSE '申怡读书' END AS order_channel,
        CASE WHEN MAX(w.product_title) LIKE '申怡甄选%' THEN '迈科' ELSE '自营' END AS sale_type,
        '视频号' AS platform,
        MIN(w.create_time) AS order_time,
        IF(MAX(w.status = '200'), 1, 0) AS is_refund
    FROM wx_order w
    WHERE w.create_time >= @start_at
      AND w.create_time < @end_at
    GROUP BY w.order_id

    UNION ALL

    /* 小红书。 */
    SELECT
        x.order_no,
        NULL AS app_order_no,
        NULL AS source_user_no,
        GROUP_CONCAT(DISTINCT x.sku_name ORDER BY x.sku_name SEPARATOR ' + ') AS product_name,
        SUM(x.price * x.quantity) / 100 AS amount,
        COALESCE(NULLIF(TRIM(MAX(x.author_name)), ''), '申怡读书') AS order_channel,
        CASE
            WHEN TRIM(IFNULL(MAX(x.author_name), '')) = '申怡读书' THEN '自营'
            WHEN IFNULL(MAX(x.author_id), '') NOT IN ('', '0') THEN '达人'
            ELSE '自营'
        END AS sale_type,
        '小红书' AS platform,
        MIN(COALESCE(x.order_create_time, x.pay_time, x.create_time)) AS order_time,
        IF(MAX(x.order_status = 9), 1, 0) AS is_refund
    FROM xhs_order x
    WHERE COALESCE(x.order_create_time, x.pay_time, x.create_time) >= @start_at
      AND COALESCE(x.order_create_time, x.pay_time, x.create_time) < @end_at
    GROUP BY x.order_no

    UNION ALL

    /* 小鹅通。 */
    SELECT
        e.order_no,
        NULL AS app_order_no,
        NULL AS source_user_no,
        GROUP_CONCAT(DISTINCT e.sku_name ORDER BY e.sku_name SEPARATOR ' + ') AS product_name,
        SUM(e.price * e.quantity) / 100 AS amount,
        '申怡读书' AS order_channel,
        '自营' AS sale_type,
        '小鹅通' AS platform,
        MIN(COALESCE(e.order_create_time, e.pay_time, e.create_time)) AS order_time,
        IF(MAX(e.order_status = 99), 1, 0) AS is_refund
    FROM xiaoe_order e
    WHERE COALESCE(e.order_create_time, e.pay_time, e.create_time) >= @start_at
      AND COALESCE(e.order_create_time, e.pay_time, e.create_time) < @end_at
    GROUP BY e.order_no
) o
/* 外部订单通过 third_party_order_no 关联 APP 开课记录。 */
LEFT JOIN (
    SELECT
        u.third_party_order_no,
        MAX(u.user_no) AS user_no,
        MIN(COALESCE(u.pay_time, u.create_time)) AS open_time,
        MIN(u.order_no) AS app_order_no
    FROM user_order u
    WHERE u.third_party_order_no IS NOT NULL
      AND u.third_party_order_no <> ''
    GROUP BY u.third_party_order_no
) um ON um.third_party_order_no = o.order_no
LEFT JOIN user_info ui ON ui.user_no = COALESCE(o.source_user_no, um.user_no)
/* 首购只使用 user_order 中最终有效且金额大于70元的历史订单。 */
LEFT JOIN (
    SELECT
        f.user_no,
        STR_TO_DATE(LEFT(f.first_sort_key, 14), '%Y%m%d%H%i%s') AS first_purchase_time,
        SUBSTRING(f.first_sort_key, 16) AS first_app_order_no
    FROM (
        SELECT
            user_no,
            MIN(CONCAT(
                DATE_FORMAT(COALESCE(pay_time, create_time), '%Y%m%d%H%i%s'),
                '|', order_no
            )) AS first_sort_key
        FROM user_order
        WHERE order_status = 1
          AND price > 70
        GROUP BY user_no
    ) f
) fp ON fp.user_no = COALESCE(o.source_user_no, um.user_no)
ORDER BY o.order_time, o.platform, o.order_no;
