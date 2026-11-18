#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日贵金属 + 主流基金涨跌幅快照，支持推送到飞书群机器人。

用法:
    python3 market_snapshot.py --mode am                  # 只打印
    python3 market_snapshot.py --mode pm --push           # 打印并推飞书
    python3 market_snapshot.py --mode pm --push --dry-run # 只构造消息不发送

环境变量:
    FEISHU_WEBHOOK   飞书群机器人 webhook 地址（--push 时必需）

输出:
    stdout 打印 Markdown 报告；归档到 reports/YYYY-MM-DD-{mode}.md

数据源:
    新浪财经行情接口（hq.sinajs.cn），需 Referer 头，GBK 编码
"""

import argparse
import json
import os
import re
import ssl
import sys
import unicodedata
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

# macOS 系统 Python 常缺根证书，优先用 certifi 的 CA 包
try:
    import certifi

    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    SSL_CONTEXT = ssl.create_default_context()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_DIR = os.path.join(BASE_DIR, "reports")

# GitHub Actions runner 是 UTC，必须显式指定东八区，否则报告时间差 8 小时
TZ = ZoneInfo("Asia/Shanghai")

SINA_URL = "https://hq.sinajs.cn/list={codes}"
HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
}

# ---------------- 财经新闻 ----------------
NEWS_API = ("https://zhibo.sina.com.cn/api/zhibo/feed"
            "?page={page}&page_size=50&zhibo_id=152&tag_id=0&dire=f&dpc=1")

# 只要跟钱和政策有关，宁缺毋滥
NEWS_KEEP = ["央行", "人民银行", "证监会", "金融监管", "银保监", "财政部", "发改委",
             "统计局", "国务院", "降准", "降息", "加息", "LPR", "CPI", "PPI", "GDP",
             "关税", "税收", "监管", "新规", "条例", "货币政策", "财政政策",
             "指数", "股指", "汇率", "人民币", "国债", "利率", "经济数据", "社融",
             "进出口", "房价", "楼市", "信贷", "存款", "保险", "养老", "社保"]

# 排除纯政治/非财经，避免信息噪音
NEWS_DROP = ["习近平", "普京", "国台办", "台湾", "外交", "会晤", "元首", "总书记",
             "政治局", "人大", "政协", "选举", "战争", "军事", "地震", "台风", "爆炸",
             "车祸", "犯罪", "判决", "明星", "娱乐", "体育", "奥运"]

KNOWLEDGE_FILE = os.path.join(BASE_DIR, "data", "daily-knowledge.json")
WORDS_FILE = os.path.join(BASE_DIR, "data", "daily-words.json")


def _http_json(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


# 新闻分类：首个命中即为该类
NEWS_CATEGORIES = [
    ("政策", ["证监会", "人民银行", "央行", "金融监管", "银保监", "财政部", "发改委",
              "国务院", "监管", "新规", "条例", "政策", "征求意见", "工作会议",
              "座谈会", "通知", "办法", "规定", "试点"]),
    ("宏观", ["GDP", "CPI", "PPI", "社融", "信贷", "进出口", "外贸", "外汇储备",
              "经济数据", "增速", "通胀", "通缩", "利率", "LPR", "降准", "降息",
              "加息", "国债", "赤字", "就业", "制造业", "PMI", "工业增加值"]),
    ("科技", ["人工智能", "AI", "算力", "芯片", "半导体", "机器人", "新能源",
              "电动车", "光伏", "储能", "5G", "6G", "量子", "大模型", "数据中心",
              "云计算", "自动驾驶", "低空", "卫星", "生物医药", "创新药"]),
    ("市场", ["指数", "A股", "港股", "美股", "汇率", "债市", "期货", "现货",
              "黄金", "原油", "涨停", "跌停", "成交量", "资金", "北向", "ETF",
              "板块", "概念", "涨超", "跌超", "收涨", "收跌", "报价", "中签"]),
    ("民生", ["养老", "社保", "医保", "房价", "楼市", "居民收入", "教育",
              "生育", "物价", "租房", "公积金", "退休", "个人所得税",
              "社会消费品", "就业率", "消费券", "以旧换新"]),
    ("微观", ["公司", "集团", "股份", "上市", "并购", "增持", "回购", "业绩",
              "签约", "中标", "产能", "出厂价"]),
]

NEWS_STATE_FILE = os.path.join(BASE_DIR, "data", "news-state.json")
_PENDING_NEWS_KEYS = []   # 构建消息时暂存，推送成功后才落盘
NEWS_KEEP_MAX = 300      # 去重记录最多保留多少条


def load_news_state():
    try:
        with open(NEWS_STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {"pushed": []}


def save_news_state(state):
    os.makedirs(os.path.dirname(NEWS_STATE_FILE), exist_ok=True)
    pushed = state.get("pushed", [])[-NEWS_KEEP_MAX:]
    with open(NEWS_STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump({"pushed": pushed}, fh, ensure_ascii=False, indent=1)


def classify_news(title):
    """返回新闻所属类别，命中不了返回 None。"""
    for name, kws in NEWS_CATEGORIES:
        if any(k in title for k in kws):
            return name
    return None


def fetch_news(per_cat=2, max_total=8, max_len=40):
    """抓新浪财经 7x24，按类别均衡挑选，并剔除已推送过的。

    返回 (items, new_titles)
      items = [(类别, 时间, 标题)]
      new_titles = 本次要记入去重库的标题
    标题原样引用，只截断不改写。
    """
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36")
    items = []
    for page in (1, 2, 3):
        try:
            d = _http_json(NEWS_API.format(page=page), {"User-Agent": ua})
            items += d["result"]["data"]["feed"]["list"]
        except Exception:  # noqa: BLE001
            continue

    seen_pushed = set(load_news_state().get("pushed", []))
    by_cat = {}
    seen_title = set()

    for it in items:
        raw = (it.get("rich_text") or "").strip()
        if not raw:
            continue
        m = re.search(r"【(.+?)】", raw)
        title = (m.group(1) if m else raw[:60]).strip()
        title = re.sub(r"\s+", " ", title)

        if len(title) < 8 or title in seen_title:
            continue
        if any(w in title for w in NEWS_DROP):
            continue
        if title in seen_pushed:          # 早晚推送去重
            continue

        cat = classify_news(title)
        if not cat:
            continue

        bucket = by_cat.setdefault(cat, [])
        if len(bucket) >= per_cat:
            continue

        seen_title.add(title)
        ct = (it.get("create_time") or "")
        tstr = ct[5:16] if len(ct) >= 16 else ct
        shown = title[:max_len] + "…" if len(title) > max_len else title
        bucket.append((cat, tstr, shown, title))

    # 按类别顺序输出，每类最多 per_cat 条
    order = [c for c, _ in NEWS_CATEGORIES]
    out, keys = [], []
    for cat in order:
        for cat_name, tstr, shown, full in by_cat.get(cat, [])[:per_cat]:
            if len(out) >= max_total:
                break
            out.append((cat_name, tstr, shown))
            keys.append(full)
        if len(out) >= max_total:
            break

    return out, keys


def load_daily_knowledge(now):
    """按日期轮换取一条投资知识，内容来自固定审核库。"""
    try:
        db = json.load(open(KNOWLEDGE_FILE, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not db:
        return None
    idx = (now.timetuple().tm_yday - 1) % len(db)
    return db[idx]


def load_daily_words(now):
    """按日期轮换取一组 5 个英文单词，内容来自固定词库。"""
    try:
        db = json.load(open(WORDS_FILE, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    if not db:
        return []
    idx = (now.timetuple().tm_yday - 1) % len(db)
    return db[idx]


# 金价分段预警（上海金 Au(T+D)，元/克）
# 跌破哪一档就在进入更深一档时提醒一次，只报新低，避免反复打扰
GOLD_ALERT_TIERS = [800.0, 750.0, 700.0]
GOLD_ALERT_STATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "data", "gold-alert-state.json")

TIER_LABEL = {
    1: "进入 700 多区间",
    2: "跌破 750",
    3: "跌破 700，到你的目标价",
}


def price_to_tier(price):
    """0=还没到；1=跌破800；2=跌破750；3=跌破700"""
    tier = 0
    for threshold in GOLD_ALERT_TIERS:
        if price <= threshold:
            tier += 1
    return tier


def read_alert_state():
    try:
        with open(GOLD_ALERT_STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {"max_tier": 0, "history": []}


def write_alert_state(state):
    os.makedirs(os.path.dirname(GOLD_ALERT_STATE), exist_ok=True)
    with open(GOLD_ALERT_STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)

# 交易日历：A股节假日不硬编码，靠数据日期比对识别休市


def fetch_raw(codes, timeout=15, retries=3):
    """批量拉取新浪行情，返回 {code: [字段...]}。失败重试。"""
    url = SINA_URL.format(codes=",".join(codes))
    last_err = None
    text = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
                text = resp.read().decode("gbk", errors="replace")
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt == retries - 1:
                raise RuntimeError(f"行情拉取失败: {last_err}") from exc

    out = {}
    for line in text.splitlines():
        m = re.match(r'var hq_str_([^=]+)="(.*)";', line.strip())
        if not m:
            continue
        key, payload = m.group(1), m.group(2)
        if not payload:
            continue
        out[key] = payload.split(",")

    if not out:
        raise RuntimeError("行情接口返回空数据（可能被限流或接口变更）")
    return out


def to_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fmt_pct(value):
    return "—" if value is None else f"{value:+.2f}%"


def fmt_price(value, digits=2):
    return "—" if value is None else f"{value:,.{digits}f}"


def disp_width(text):
    """终端/飞书显示宽度：CJK 全角字符占 2 列。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def pad(text, width, align="left"):
    gap = max(0, width - disp_width(text))
    return text + " " * gap if align == "left" else " " * gap + text


def arrow(value):
    if value is None:
        return "⚪"
    return "🔴" if value > 0 else "🟢" if value < 0 else "⚪"


# ---------------------------------------------------------------- 数据时效

def check_freshness(raw, now):
    """
    用数据源自带的时间戳判断行情是否新鲜。
    返回 (quote_dt, warning)，quote_dt 为 None 表示解析不到。
    """
    quote_dt = None
    f = raw.get("hf_XAU") or raw.get("gds_AUTD")
    if f and len(f) > 12:
        # hf_/gds_ 格式: [6]=时间 HH:MM:SS, [12]=日期 YYYY-MM-DD
        try:
            quote_dt = datetime.strptime(
                f"{f[12]} {f[6]}", "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=TZ)
        except (ValueError, IndexError):
            quote_dt = None

    if quote_dt is None:
        return None, "⚠️ 无法识别行情时间，请人工核对数据是否为最新"

    gap_min = (now - quote_dt).total_seconds() / 60

    if gap_min > 180:
        hours = gap_min / 60
        return quote_dt, (
            f"⚠️ 行情数据停在 {quote_dt:%m-%d %H:%M}，距当前 {hours:.1f} 小时——"
            "多为休市或数据源延迟，勿当作实时行情"
        )
    if gap_min > 60:
        return quote_dt, (
            f"⚠️ 行情时间为 {quote_dt:%H:%M}，已滞后 {gap_min:.0f} 分钟"
        )
    return quote_dt, ""


# ---------------------------------------------------------------- 各板块解析

def parse_gold_silver(raw):
    """伦敦现货金银 + 国内上金所延期"""
    rows = []

    # 国际: hf_XAU / hf_XAG -> [0] 现价, [1] 昨收
    for code, name in (("hf_XAU", "伦敦金 XAU"), ("hf_XAG", "伦敦银 XAG")):
        f = raw.get(code)
        if not f:
            rows.append((name, "美元/盎司", None, None))
            continue
        price, prev = to_float(f[0]), to_float(f[1])
        change = (price - prev) / prev * 100 if price and prev else None
        digits = 3 if "XAG" in code else 2
        rows.append((name, "美元/盎司", price, change, digits))

    # 国内: gds_AUTD / gds_AGTD -> [0] 现价, [7] 昨收
    for code, name in (("gds_AUTD", "黄金延期 Au(T+D)"), ("gds_AGTD", "白银延期 Ag(T+D)")):
        f = raw.get(code)
        if not f or len(f) <= 7:
            rows.append((name, "元/克" if "AUTD" in code else "元/千克", None, None, 2))
            continue
        price, prev = to_float(f[0]), to_float(f[7])
        change = (price - prev) / prev * 100 if price and prev else None
        rows.append((
            name,
            "元/克" if "AUTD" in code else "元/千克",
            price, change,
            2 if "AUTD" in code else 0,
        ))

    return rows


def parse_domestic_index(raw):
    """A股指数 + 对应 ETF。s_ 接口: 名称, 现价, 涨跌额, 涨跌幅%, 成交量, 成交额"""
    pairs = [
        ("s_sh000001", "上证指数", None),
        ("s_sh000300", "沪深300", "s_sh510300"),
        ("s_sh000905", "中证500", "s_sh510500"),
        ("s_sz399006", "创业板指", "s_sz159915"),
        ("s_sh000688", "科创50", "s_sh588000"),
    ]
    rows = []
    for idx_code, idx_name, etf_code in pairs:
        f = raw.get(idx_code)
        if not f or len(f) < 4:
            rows.append((idx_name, None, None, etf_code, None, None, "取数失败"))
            continue
        idx_price, idx_chg = to_float(f[1]), to_float(f[3])

        etf_price = etf_chg = None
        note = ""
        if etf_code:
            ef = raw.get(etf_code)
            if ef and len(ef) >= 4:
                etf_price, etf_chg = to_float(ef[1]), to_float(ef[3])
                if not (to_float(ef[4]) if len(ef) > 4 else None):
                    note = "未成交"  # 停牌或未开盘，报价为前收
            else:
                note = "取数失败"
        rows.append((idx_name, idx_price, idx_chg, etf_code, etf_price, etf_chg, note))
    return rows


def parse_cross_border(raw):
    """恒生科技(rt_hkHSTECH) / 纳指100(gb_$ndx) + 对应ETF"""
    rows = []

    f = raw.get("rt_hkHSTECH")
    price = chg = None
    if f and len(f) > 8:
        price, chg = to_float(f[6]), to_float(f[8])
    etf_price = etf_chg = None
    note = ""
    ef = raw.get("s_sh513130")
    if ef and len(ef) >= 4:
        etf_price, etf_chg = to_float(ef[1]), to_float(ef[3])
        if not (to_float(ef[4]) if len(ef) > 4 else None):
            note = "未成交"
    rows.append(("恒生科技", price, chg, "s_sh513130", etf_price, etf_chg, note))

    f = raw.get("gb_$ndx")
    price = chg = None
    if f and len(f) > 2:
        price, chg = to_float(f[1]), to_float(f[2])
    etf_price = etf_chg = None
    note = ""
    ef = raw.get("s_sh513100")
    if ef and len(ef) >= 4:
        etf_price, etf_chg = to_float(ef[1]), to_float(ef[3])
        if not (to_float(ef[4]) if len(ef) > 4 else None):
            note = "未成交"
    rows.append(("纳斯达克100", price, chg, "s_sh513100", etf_price, etf_chg, note))

    return rows


# ---------------------------------------------------------------- 渲染

def build_report(mode, data, now, quote_dt, warning):
    is_am = mode == "am"
    title = "盘前速览" if is_am else "收盘复盘"

    metals, domestic, cross = data["metals"], data["domestic"], data["cross"]
    weekend = now.weekday() >= 5

    lines = [
        f"# {now:%Y-%m-%d} 贵金属 & 基金涨跌 · {title}",
        "",
        f"> 生成时间 {now:%H:%M}　|　行情时间 {quote_dt:%m-%d %H:%M}　|　数据源 新浪财经",
    ]
    if warning:
        lines.append(f"> {warning}")
    if weekend:
        lines.append("> ⚠️ 今日周末休市，以下为最近一个交易日数据")
    elif is_am:
        lines.append("> A股尚未开盘，指数/ETF 为上一交易日收盘；国际金银为隔夜连续报价")
    lines.append("")

    lines.append("## 一、贵金属")
    lines.append("")
    lines.append("| 品种 | 最新价 | 涨跌幅 | 单位 |")
    lines.append("|---|---:|---:|---|")
    for name, unit, price, change, digits in metals:
        lines.append(
            f"| {name} | {fmt_price(price, digits)} | {arrow(change)} {fmt_pct(change)} | {unit} |"
        )
    lines.append("")

    for section, rows in (("二、A股主要指数 / 对应ETF", domestic), ("三、跨境（港股 / 美股）", cross)):
        lines.append(f"## {section}")
        lines.append("")
        lines.append("| 指数 | 最新 | 涨跌幅 | ETF代码 | ETF最新 | ETF涨跌幅 |")
        lines.append("|---|---:|---:|---|---:|---:|")
        for name, ip, ic, etf_code, ep, ec, note in rows:
            code_cell = etf_code.replace("s_sh", "").replace("s_sz", "") if etf_code else "—"
            if ip is None:
                lines.append(f"| {name} | — | — | {code_cell} | — | — |")
                continue
            if note == "未成交":
                chg_cell = f"⚪ 未成交（前收 {fmt_price(ep, 3)}）"
            elif ep is None:
                chg_cell = "—"
            else:
                chg_cell = f"{arrow(ec)} {fmt_pct(ec)}"
            lines.append(
                f"| {name} | {fmt_price(ip)} | {arrow(ic)} {fmt_pct(ic)} "
                f"| {code_cell} | {fmt_price(ep, 3)} | {chg_cell} |"
            )
        lines.append("")
        if section.startswith("三"):
            lines.append("> ETF 为 A股场内对应品种，折溢价会使涨跌幅与指数小幅偏离；跨境ETF 因开市时间差异可能滞后。")
            lines.append("")

    lines.append("## 小结")
    lines.append("")
    gold = next((r for r in metals if "XAU" in r[0]), None)
    gold_dom = next((r for r in metals if "Au(T+D)" in r[0]), None)
    a_chg = [r[2] for r in domestic if r[2] is not None]
    avg = sum(a_chg) / len(a_chg) if a_chg else None

    bits = []
    if gold and gold[3] is not None:
        bits.append(f"伦敦金 {fmt_pct(gold[3])}")
    if gold_dom and gold_dom[3] is not None:
        bits.append(f"国内金 {fmt_pct(gold_dom[3])}")
    if avg is not None:
        direction = "普涨" if avg > 0.5 else "普跌" if avg < -0.5 else "涨跌互现"
        bits.append(f"A股主要宽基均值 {fmt_pct(avg)}（{direction}）")
    lines.append("　·　".join(bits) if bits else "数据获取不完整，请检查网络或数据源。")
    lines.append("")

    return "\n".join(lines)


def build_comment(data, mode="pm"):
    """两句盘面判断：客观描述 + 对定投的影响。只依据当天数据，不做预测。

    盘前（am）时 A股 尚未开盘，其涨跌幅是上一交易日数据，不纳入描述。
    """
    metals, domestic, cross = data["metals"], data["domestic"], data["cross"]

    gold = next((r[3] for r in metals if "XAU" in r[0]), None)
    silver = next((r[3] for r in metals if "XAG" in r[0]), None)
    a_list = [r[2] for r in domestic if r[2] is not None]
    a_avg = sum(a_list) / len(a_list) if a_list else None
    ndx_row = next((r for r in cross if "纳斯达克" in r[0]), None)
    ndx = ndx_row[2] if ndx_row else None

    # ---- 第一句：盘面方向 ----
    parts = []
    if gold is not None and silver is not None:
        if gold > 0 and silver > 0:
            parts.append("金银同涨")
        elif gold < 0 and silver < 0:
            parts.append("金银同跌")
        else:
            parts.append("金银分化")
    if a_avg is not None and mode == "pm":
        if a_avg > 0.5:
            parts.append(f"A股普涨（均值{fmt_pct(a_avg)}）")
        elif a_avg < -0.5:
            parts.append(f"A股普跌（均值{fmt_pct(a_avg)}）")
        else:
            parts.append(f"A股分化（均值{fmt_pct(a_avg)}）")
    if ndx is not None:
        if ndx < -1:
            parts.append(f"纳指重挫 {fmt_pct(ndx)}")
        elif ndx > 1:
            parts.append(f"纳指大涨 {fmt_pct(ndx)}")
        else:
            parts.append(f"纳指 {fmt_pct(ndx)}")
    line1 = "，".join(parts) + "。"

    # ---- 第二句：避险还是risk-on ----
    mood = ""
    if gold is not None and a_avg is not None and mode == "pm":
        if gold > 0 and a_avg < 0:
            mood = "金涨股跌，避险情绪偏重"
        elif gold < 0 and a_avg > 0:
            mood = "金跌股涨，风险偏好回升"
        elif gold > 0 and a_avg > 0:
            mood = "金银与股票同涨，资金面偏宽松"
        elif gold < 0 and a_avg < 0:
            mood = "金银与股票同跌，普遍在去杠杆"

    line2 = ""
    if ndx is not None:
        if ndx < -1:
            line2 = "美股跌幅较大，纳指/标普日投照常扣款，同样的钱买到更多份额。"
        elif ndx > 1.5:
            line2 = "美股大涨，日投买到的份额变贵，这是定投的常态代价，不必回避。"
    if mood:
        line2 = (mood + "。" + line2) if line2 else (mood + "。")

    return line1, line2


# 飞书消息里的简称
FEISHU_SHORT = {
    "伦敦金 XAU": "伦敦金",
    "伦敦银 XAG": "伦敦银",
    "黄金延期 Au(T+D)": "上海金",
    "白银延期 Ag(T+D)": "上海银",
    "纳斯达克100": "纳指100",
}
# A股只报涨跌幅，不出点位；不含上证指数（与沪深300高度重合，且无对应基金标的）
FEISHU_INDEX = ["沪深300", "中证500", "创业板指", "科创50"]


def build_feishu_text(mode, data, now, quote_dt, warning, extra=True):
    """飞书消息：极简版，一行一个标的，只报涨跌幅。"""
    title = "盘前" if mode == "am" else "收盘"
    metals, domestic, cross = data["metals"], data["domestic"], data["cross"]

    # 标题必须含"金银"：飞书机器人安全设置里以它为关键词，缺了会被拒收（code 19024）
    lines = [f"📊 金银 · {now:%m-%d} {title}"]
    if warning:
        lines.append(warning)
    if now.weekday() >= 5:
        lines.append("周末休市，为最近交易日数据")

    lines.append("")
    for name, unit, price, change, digits in metals:
        label = FEISHU_SHORT.get(name, name)
        lines.append(
            f"{arrow(change)} {pad(label, 7)}"
            f"{pad(fmt_price(price, digits), 10, 'right')}  {fmt_pct(change)}"
        )

    lines.append("")
    for name, ip, ic, etf_code, ep, ec, note in domestic:
        if name not in FEISHU_INDEX:
            continue
        lines.append(f"{arrow(ic)} {pad(name, 10)}{fmt_pct(ic)}")

    for name, ip, ic, etf_code, ep, ec, note in cross:
        lines.append(f"{arrow(ic)} {pad(FEISHU_SHORT.get(name, name), 10)}{fmt_pct(ic)}")

    # 金价预警：显示下一档距离；已进入的档位在顶部提示
    gold_price = next((r[2] for r in metals if "Au(T+D)" in r[0]), None)
    if gold_price and gold_price > 0:
        tier = price_to_tier(gold_price)
        if tier > 0:
            lines.insert(0, f"🚨 上海金 {fmt_price(gold_price)} · {TIER_LABEL[tier]}")
        if tier < len(GOLD_ALERT_TIERS):
            nxt = GOLD_ALERT_TIERS[tier]
            gap = (gold_price - nxt) / gold_price * 100
            lines.append(f"距 {nxt:.0f} 元/克还有 -{gap:.1f}%")

    # ---- 财经要闻 / 今日知识 / 今日单词 ----
    if extra:
        news, new_keys = fetch_news()
        _PENDING_NEWS_KEYS.clear()
        _PENDING_NEWS_KEYS.extend(new_keys)
        if news:
            lines.append("")
            lines.append("📰 财经要闻")
            for cat, tstr, title in news:
                lines.append(f"【{cat}】{tstr} {title}")

        k = load_daily_knowledge(now)
        if k:
            lines.append("")
            lines.append("💡 今日知识")
            lines.append(f"{k['t']}：{k['d']}")

        ws = load_daily_words(now)
        if ws:
            lines.append("")
            lines.append("🔤 今日单词")
            for w, pos, cn in ws:
                lines.append(f"{pad(w, 20)}{pad(pos, 10)}{cn}")

    line1, line2 = build_comment(data, mode)
    lines.append("")
    lines.append(line1)
    if line2:
        lines.append(line2)

    return "\n".join(lines).strip()


# ---------------------------------------------------------------- 推送

def _post_json(url, payload, timeout=20):
    """POST JSON，返回 (成功, 响应或错误信息)"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def push_feishu(webhook, text, dry_run=False):
    """飞书群机器人（自定义机器人）"""
    if dry_run:
        print("\n--- 飞书消息预览（dry-run，未发送）---", file=sys.stderr)
        print(text, file=sys.stderr)
        return True

    ok, result = _post_json(webhook, {"msg_type": "text", "content": {"text": text}})
    if not ok:
        print(f"❌ 飞书推送失败: {result}", file=sys.stderr)
        return False

    code = result.get("code", result.get("StatusCode", -1))
    if code not in (0, None) and str(code) != "0":
        print(f"❌ 飞书返回错误: {result}", file=sys.stderr)
        return False
    print("✅ 飞书推送成功", file=sys.stderr)
    return True


def push_wecom(webhook, text, dry_run=False):
    """企业微信群机器人。注意：个人微信群无官方 API，用不了这个。"""
    if dry_run:
        print("\n--- 企业微信消息预览（dry-run，未发送）---", file=sys.stderr)
        print(text, file=sys.stderr)
        return True

    ok, result = _post_json(webhook, {"msgtype": "text", "text": {"content": text}})
    if not ok:
        print(f"❌ 企业微信推送失败: {result}", file=sys.stderr)
        return False

    code = result.get("errcode", -1)
    if code != 0:
        print(f"❌ 企业微信返回错误: {result}", file=sys.stderr)
        return False
    print("✅ 企业微信推送成功", file=sys.stderr)
    return True


def push_pushplus(token, text, dry_run=False):
    """PushPlus：消息推送到微信（关注公众号即可收，无需装 App）"""
    if dry_run:
        print("\n--- PushPlus 微信消息预览（dry-run，未发送）---", file=sys.stderr)
        print(text, file=sys.stderr)
        return True

    # 标题取首行（PushPlus 标题过长会被截断）
    title = text.strip().splitlines()[0].lstrip("📊 ").strip() or "行情推送"
    payload = {
        "token": token,
        "title": title[:40],
        # markdown 模板：用代码块包住正文，保住等宽对齐
        "content": "```\n" + text + "\n```",
        "template": "markdown",
    }
    ok, result = _post_json("https://www.pushplus.plus/send", payload)
    if not ok:
        print(f"❌ PushPlus 推送失败: {result}", file=sys.stderr)
        return False

    code = result.get("code")
    if code != 200:
        print(f"❌ PushPlus 返回错误: {result}", file=sys.stderr)
        return False
    print("✅ PushPlus(微信) 推送成功", file=sys.stderr)
    return True


def push_all(text, dry_run=False):
    """按配置推送到所有已启用的通道。任一失败不影响其他。"""
    channels = [
        ("飞书", "FEISHU_WEBHOOK", push_feishu),
        ("企业微信", "WECOM_WEBHOOK", push_wecom),
        ("PushPlus微信", "PUSHPLUS_TOKEN", push_pushplus),
    ]
    enabled = [(name, os.environ.get(env, "").strip(), fn)
               for name, env, fn in channels]
    enabled = [(n, u, f) for n, u, f in enabled if u]

    if not enabled:
        if dry_run:
            push_feishu("", text, dry_run=True)
            return True
        print("❌ 未配置任何推送通道（FEISHU_WEBHOOK / WECOM_WEBHOOK）", file=sys.stderr)
        return False

    results = [fn(url, text, dry_run=dry_run) for _, url, fn in enabled]
    return all(results)


# ---------------------------------------------------------------- 主流程

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["am", "pm"], default="pm")
    parser.add_argument("--push", action="store_true", help="推送到飞书群机器人")
    parser.add_argument("--dry-run", action="store_true", help="只构造消息不发送")
    parser.add_argument("--no-save", action="store_true", help="不归档到 reports/")
    parser.add_argument("--alert-only", action="store_true",
                        help="仅当金价跌破预警线时才推送，未触发则静默退出")
    args = parser.parse_args()

    codes = [
        "hf_XAU", "hf_XAG", "gds_AUTD", "gds_AGTD",
        "s_sh000001", "s_sh000300", "s_sh000905", "s_sz399006", "s_sh000688",
        "s_sh510300", "s_sh510500", "s_sz159915", "s_sh588000",
        "rt_hkHSTECH", "s_sh513130", "gb_$ndx", "s_sh513100",
    ]

    try:
        raw = fetch_raw(codes)
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    now = datetime.now(TZ)
    quote_dt, warning = check_freshness(raw, now)

    data = {
        "metals": parse_gold_silver(raw),
        "domestic": parse_domestic_index(raw),
        "cross": parse_cross_border(raw),
    }

    # 预警模式：只在进入更深一档时推送，否则静默
    if args.alert_only:
        gold_price = next((r[2] for r in data["metals"] if "Au(T+D)" in r[0]), None)
        if gold_price is None:
            print("❌ 未取到金价，跳过本次检查", file=sys.stderr)
            return 0

        tier = price_to_tier(gold_price)
        state = read_alert_state()
        prev_max = int(state.get("max_tier", 0))

        print(f"金价 {gold_price:.2f} 元/克 → 当前档位 {tier}（历史最深 {prev_max}）", file=sys.stderr)
        if tier <= prev_max:
            print("未创新低，本轮不推送", file=sys.stderr)
            return 0

        print(f"🚨 进入新档位 {tier}（{TIER_LABEL[tier]}），触发推送", file=sys.stderr)

        text = build_feishu_text(args.mode, data, now, quote_dt, warning, extra=False)
        if not push_all(text, dry_run=args.dry_run):
            return 1

        state["max_tier"] = tier
        state.setdefault("history", []).append(
            {"date": now.strftime("%Y-%m-%d %H:%M"), "price": gold_price, "tier": tier}
        )
        write_alert_state(state)
        print(f"状态已更新：max_tier={tier}", file=sys.stderr)
        return 0

    report = build_report(args.mode, data, now, quote_dt, warning)
    print(report)

    if not args.no_save:
        os.makedirs(REPORT_DIR, exist_ok=True)
        path = os.path.join(REPORT_DIR, f"{now:%Y-%m-%d}-{args.mode}.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print(f"\n_已归档: {path}_", file=sys.stderr)

    if args.push:
        text = build_feishu_text(args.mode, data, now, quote_dt, warning)
        if not push_all(text, dry_run=args.dry_run):
            return 1
        # 推送成功才写去重记录；失败则下次重发同批新闻
        if _PENDING_NEWS_KEYS and not args.dry_run:
            st = load_news_state()
            st.setdefault("pushed", []).extend(_PENDING_NEWS_KEYS)
            save_news_state(st)
            print(f"✅ 已记录 {len(_PENDING_NEWS_KEYS)} 条新闻去重", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
