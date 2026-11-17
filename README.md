# 赚钱计划 · 每日行情推送

每天定时推送贵金属（金银）与主流基金/指数涨跌幅，走 **GitHub Actions 云端调度 + 飞书群机器人**，不依赖本机开机。

## 为什么用云端

DSH 内置的定时任务是本机进程内调度（任务存 `~/.dsh/storages/schedule.json`），
电脑关机 / 合盖睡眠 / DSH 退出都不触发，且重开后只补最近一次，
补跑时拿到的是**当时的实时行情**而非目标时点行情，会造成数据与标签错配。
因此改用 GitHub Actions：云端定时，与本地开关机无关。

## 推送时间

| 时间（北京） | 内容 | cron (UTC) |
|---|---|---|
| 每天 08:30 | 盘前速览：隔夜伦敦金银 + 港美股收盘 | `30 0 * * *` |
| 每天 21:00 | 收盘复盘：全量 | `0 13 * * *` |

## 标的清单

| 板块 | 标的 |
|---|---|
| 贵金属·国际 | 伦敦金 XAU、伦敦银 XAG（美元/盎司） |
| 贵金属·国内 | 黄金延期 Au(T+D)（元/克）、白银延期 Ag(T+D)（元/千克） |
| A股宽基 | 上证指数、沪深300(510300)、中证500(510500)、创业板指(159915)、科创50(588000) |
| 跨境 | 恒生科技(513130)、纳斯达克100(513100) |

每行给出指数涨跌幅与对应 ETF 涨跌幅，两者因折溢价会有小幅偏离。

## 部署步骤

1. 在 GitHub 建**私有**仓库，把本目录推上去。
2. 仓库 Settings → Secrets and variables → Actions → New repository secret
   - Name: `FEISHU_WEBHOOK`
   - Value: 飞书群机器人 webhook 地址
3. Actions 页面手动 Run workflow 各跑一次，确认能收到消息。
4. 之后每天定时自动跑，报告同时归档在 `reports/` 并由 workflow 提交回仓库。

### 建飞书机器人

飞书群 → 设置 → 群机器人 → 添加机器人 → **自定义机器人** → 复制 webhook 地址。
安全设置建议勾选「自定义关键词」，填 `金银`（脚本消息首行含该词）。

## 手动跑

```bash
pip install certifi

# 只看
python3 scripts/market_snapshot.py --mode am

# 推飞书（需 webhook）
FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/xxx" \
  python3 scripts/market_snapshot.py --mode pm --push

# 预览消息不发送
python3 scripts/market_snapshot.py --mode pm --push --dry-run

# 不归档
python3 scripts/market_snapshot.py --mode pm --no-save
```

## 数据源与容错

- **新浪财经** `hq.sinajs.cn`，免费无密钥。已处理 GBK 编码、Referer 校验、SSL 根证书（certifi）、3 次重试。
- **时区**：`zoneinfo` 显式锁定 `Asia/Shanghai`，GitHub runner 是 UTC 也不会错 8 小时。
- **数据时效校验**：解析数据源自带时间戳，若落后当前时间超 60 分钟提示延迟，超 3 小时判定休市/陈旧，消息首部直接告警，杜绝「补投数据当实时」。
- **ETF 未成交**：跨境 ETF 在 A股时段可能无成交，显示「未成交」并补注指数口径，不会误报成 0.00%。
- **接口失效**：workflow 失败时会输出 HTTP 诊断到 Summary（含 runner 地区连通性判断）。备选数据源：东方财富 `push2.eastmoney.com`。

## 已知限制

- GitHub Actions 的 schedule 有几分钟到十几分钟延迟，偶发更久（非商用级 SLA）。
- 私有仓库 Actions 免费额度 2000 分钟/月；每天 2 次、每次约 30 秒，月耗约 1% 额度。
- 仓库连续 60 天无任何提交，GitHub 会自动停用 schedule。脚本每天提交归档报告，正常工作即自带保活；若长期报错需留意。
- 境外 runner 访问新浪财经若受限，需更换数据源（见上）。
