# Feishu Business Dashboard

飞书多维表格业务数据驾驶舱同步脚本。

## 功能

- 从 MySQL 查询各平台每日成交数据
- 按成交平台和渠道写入飞书多维表格
- 支持金额、退款后金额、订单数、退款订单数
- 支持飞书表格里的手动调整字段
- 支持服务器 cron 每小时自动执行

## 文件

- `sync_channel_daily_to_feishu.py`：主同步脚本
- `.env.example`：配置示例，不包含真实密钥
- `.gitignore`：避免提交真实配置和本地缓存

## 运行

复制配置模板：

```bash
cp .env.example .env
```

填好 `.env` 后测试：

```bash
python3 sync_channel_daily_to_feishu.py --env .env --dry-run
```

正式同步：

```bash
python3 sync_channel_daily_to_feishu.py --env .env
```

## 服务器定时任务

每小时执行一次：

```cron
0 * * * * cd /opt/feishu-business-dashboard && /usr/bin/python3 sync_channel_daily_to_feishu.py --env .env >> /var/log/feishu-business-dashboard/sync.log 2>> /var/log/feishu-business-dashboard/sync.err.log
```

## 安全

真实 `.env` 包含数据库密码和飞书密钥，不要提交到 GitHub。
