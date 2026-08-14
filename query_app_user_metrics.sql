-- MySQL 5.7，只读查询。
-- 脚本会替换 @start_at、@end_at 和 @event_table。
SET @start_at = '2026-08-01 00:00:00';
SET @end_at = '2026-08-15 00:00:00';

SELECT
    d.日期,
    COALESCE(r.注册数, 0) AS 新增注册用户数,
    COALESCE(e.APP活跃人数, 0) AS 当日APP活跃人数,
    COALESCE(e.APP学习用户人数, 0) AS 当日APP学习用户人数
FROM (
    SELECT DATE_ADD(DATE(@start_at), INTERVAL n DAY) AS 日期
    FROM (
        SELECT 0 n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
        UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9
        UNION ALL SELECT 10 UNION ALL SELECT 11 UNION ALL SELECT 12 UNION ALL SELECT 13 UNION ALL SELECT 14
        UNION ALL SELECT 15 UNION ALL SELECT 16 UNION ALL SELECT 17 UNION ALL SELECT 18 UNION ALL SELECT 19
        UNION ALL SELECT 20 UNION ALL SELECT 21 UNION ALL SELECT 22 UNION ALL SELECT 23 UNION ALL SELECT 24
        UNION ALL SELECT 25 UNION ALL SELECT 26 UNION ALL SELECT 27 UNION ALL SELECT 28 UNION ALL SELECT 29
        UNION ALL SELECT 30
    ) nums
    WHERE DATE_ADD(DATE(@start_at), INTERVAL n DAY) < DATE(@end_at)
) d
LEFT JOIN (
    SELECT DATE(create_time) AS 日期, COUNT(DISTINCT user_no) AS 注册数
    FROM user_info
    WHERE create_time >= @start_at AND create_time < @end_at
    GROUP BY DATE(create_time)
) r ON r.日期 = d.日期
LEFT JOIN (
    SELECT
        DATE(create_time) AS 日期,
        COUNT(DISTINCT CASE
            WHEN event_key IN ('APP_start_first', 'APP_start_second', 'APP_login') THEN user_no
        END) AS APP活跃人数,
        COUNT(DISTINCT CASE
            WHEN event_key IN (
                'APP_audio_play_host', 'APP_audio_play_sub',
                'APP_video_play_host', 'APP_video_play_sub'
            ) THEN user_no
        END) AS APP学习用户人数
    FROM @event_table
    WHERE create_time >= @start_at AND create_time < @end_at
    GROUP BY DATE(create_time)
) e ON e.日期 = d.日期
ORDER BY d.日期;
