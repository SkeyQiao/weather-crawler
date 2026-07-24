#!/usr/bin/env python3
"""
2345 天气网历史天气爬虫
========================
功能：
  - 自动爬取全国所有城市的历史天气数据
  - 支持 T+1 模式（每天爬前一天数据）
  - 支持历史回填（首次运行从指定日期开始补全）
  - 断点续爬（已爬过的日期不会重复爬）
  - 数据存储到 SQLite 数据库

用法：
  python3 weather_crawler.py              # T+1 模式：爬昨天的数据
  python3 weather_crawler.py --backfill   # 回填模式：从 2026-07-01 至今
  python3 weather_crawler.py --date 2026-07-05  # 爬指定日期
"""

import requests
import sqlite3
import time
import re
import os
import sys
import json
from datetime import datetime, timedelta
from html.parser import HTMLParser

# ============================================================
# 配置
# ============================================================

# 数据库文件路径（存在脚本同目录）
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather_data.db")

# 城市数据来源
CITY_DATA_URL = "https://tianqi-stream.2345cdn.net/tqpcimg/tianqiimg/theme4/js/citySelectData2.js"

# API 地址
API_URL = "https://tianqi.2345.com/Pc/GetHistory"

# 请求头（必须带这些，否则 API 会拒绝）
HEADERS = {
    "Referer": "https://tianqi.2345.com/wea_history/54662.htm",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
}

# 每次请求之间的间隔（秒），太快会被封
REQUEST_DELAY = 0.3

# 请求失败后的重试次数
MAX_RETRIES = 3

# 回填起始日期
BACKFILL_START = "2026-07-01"

# ============================================================
# 数据库初始化
# ============================================================

def init_db():
    """创建数据库和表（如果不存在）"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 天气数据表
    c.execute("""
        CREATE TABLE IF NOT EXISTS weather_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            area_id INTEGER NOT NULL,
            city_name TEXT NOT NULL,
            province TEXT NOT NULL,
            date TEXT NOT NULL,
            max_temp INTEGER,
            min_temp INTEGER,
            weather TEXT,
            wind TEXT,
            aqi INTEGER,
            aqi_level TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(area_id, date)
        )
    """)

    # 爬取日志表
    c.execute("""
        CREATE TABLE IF NOT EXISTS crawl_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_date TEXT NOT NULL,
            cities_total INTEGER,
            cities_success INTEGER,
            cities_failed INTEGER,
            status TEXT,
            started_at TEXT,
            finished_at TEXT
        )
    """)

    # 创建索引加速查询
    c.execute("CREATE INDEX IF NOT EXISTS idx_date ON weather_daily(date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_area_date ON weather_daily(area_id, date)")

    conn.commit()
    conn.close()


# ============================================================
# 城市列表获取
# ============================================================

def fetch_city_list():
    """
    从 2345 的 JS 文件中解析全国城市列表。
    返回: [(area_id, city_name, province_name), ...]
    """
    print("正在获取城市列表...")
    resp = requests.get(CITY_DATA_URL, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=30)
    resp.encoding = "utf-8"
    js_content = resp.text

    cities = []
    province_map = {}

    # 省份名称映射（prov 数组的 key 对应的中文名）
    province_names = {
        10: "安徽", 11: "澳门", 12: "北京", 13: "福建", 14: "甘肃",
        15: "广东", 16: "广西", 17: "贵州", 18: "海南", 19: "河北",
        20: "河南", 21: "黑龙江", 22: "湖北", 23: "湖南", 24: "吉林",
        25: "江苏", 26: "江西", 27: "辽宁", 28: "内蒙古", 29: "宁夏",
        30: "青海", 31: "山东", 32: "山西", 33: "陕西", 34: "上海",
        35: "四川", 36: "台湾", 37: "天津", 38: "西藏", 39: "香港",
        40: "新疆", 41: "云南", 42: "浙江", 43: "重庆",
    }

    # 直辖市/特别行政区的 areaId
    municipality_ids = {
        "北京": 54511,
        "上海": 58362,
        "天津": 54527,
        "重庆": 57516,
        "香港": 45007,
        "澳门": 45011,
        "台湾": 58974,
    }

    for prov_id, prov_name in province_names.items():
        # 尝试从 prov 数组中提取城市列表
        pattern = rf"prov\[{prov_id}\]\s*=\s*'([^']*)'"
        match = re.search(pattern, js_content)
        if not match:
            continue

        data = match.group(1)
        # 格式: '58321-H 合肥-58321|58424-A 安庆-58424|...'
        # 用 | 分割出每个城市
        city_entries = data.split("|")

        for entry in city_entries:
            # 格式: '58321-H 合肥-58321'
            # 用正则提取 areaId 和城市名
            m = re.match(r"(\d+)-[A-Z]\s+(.+?)-(\d+)", entry)
            if m:
                area_id = int(m.group(1))
                city_name = m.group(2)
                cities.append((area_id, city_name, prov_name))

        # 如果没有城市数据（直辖市），手动添加
        if not city_entries or (
            len(city_entries) == 1
            and re.match(r"\d+-[A-Z]\s+.+-\d+", city_entries[0])
            and city_entries[0].count("-") <= 3
        ):
            # 检查是否是直辖市格式：'12-B 北京-12'
            simple_match = re.match(r"(\d+)-[A-Z]\s+(.+?)-\1", data)
            if simple_match and prov_name in municipality_ids:
                area_id = municipality_ids[prov_name]
                cities.append((area_id, prov_name, prov_name))

    # 去重（按 area_id）
    seen = set()
    unique_cities = []
    for area_id, name, prov in cities:
        if area_id not in seen and area_id >= 10000:  # 过滤掉非 weather station ID
            seen.add(area_id)
            unique_cities.append((area_id, name, prov))

    print(f"共获取到 {len(unique_cities)} 个城市")
    return unique_cities


# ============================================================
# HTML 数据解析
# ============================================================

def parse_weather_html(html_str, year, month):
    """
    解析 API 返回的 HTML，提取每天的天气数据。
    返回: [(date, max_temp, min_temp, weather, wind, aqi, aqi_level), ...]
    """
    results = []

    # 提取表格行（跳过表头）
    rows = re.findall(r"<tr>\s*(.*?)</tr>", html_str, re.DOTALL)
    for row in rows:
        # 找日期
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", row)
        if not date_match:
            continue
        date = date_match.group(1)

        # 提取所有 td 内容
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)

        if len(tds) < 6:
            continue

        # 最高温（第2个td，可能有 color 样式）
        max_temp = int(re.search(r"(\d+)°", tds[1]).group(1)) if re.search(r"(\d+)°", tds[1]) else None

        # 最低温（第3个td）
        min_temp = int(re.search(r"(\d+)°", tds[2]).group(1)) if re.search(r"(\d+)°", tds[2]) else None

        # 天气（第4个td）
        weather = re.sub(r"<[^>]+>", "", tds[3]).strip()

        # 风力风向（第5个td）
        wind = re.sub(r"<[^>]+>", "", tds[4]).strip()

        # AQI 和等级（第6个td）
        aqi = None
        aqi_level = None
        aqi_text = re.sub(r"<[^>]+>", "", tds[5]).strip()
        aqi_match = re.match(r"(\d+)\s*(.+)", aqi_text)
        if aqi_match:
            aqi = int(aqi_match.group(1))
            aqi_level = aqi_match.group(2)

        results.append((date, max_temp, min_temp, weather, wind, aqi, aqi_level))

    return results


# ============================================================
# 单城市单月数据爬取
# ============================================================

def fetch_city_month(area_id, year, month):
    """
    爬取某个城市某个月的历史天气。
    返回: [(date, max_temp, min_temp, weather, wind, aqi, aqi_level), ...]
    失败返回 None
    """
    params = {
        "areaInfo[areaId]": area_id,
        "areaInfo[areaType]": 2,
        "date[year]": year,
        "date[month]": month,
    }

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=15)
            data = resp.json()

            if data.get("code") == 1 and data.get("data"):
                return parse_weather_html(data["data"], year, month)
            else:
                # code != 1 可能表示该城市该月无数据
                return []

        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(REQUEST_DELAY * 2)
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(REQUEST_DELAY * 2)
            else:
                print(f"  [错误] area_id={area_id}, {year}-{month:02d}: {e}")
                return None

    return None


# ============================================================
# 数据存储
# ============================================================

def save_weather_data(conn, area_id, city_name, province, records):
    """将解析后的天气记录存入数据库"""
    c = conn.cursor()
    saved = 0
    for date, max_t, min_t, weather, wind, aqi, aqi_level in records:
        try:
            c.execute("""
                INSERT OR IGNORE INTO weather_daily
                (area_id, city_name, province, date, max_temp, min_temp, weather, wind, aqi, aqi_level)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (area_id, city_name, province, date, max_t, min_t, weather, wind, aqi, aqi_level))
            if c.rowcount > 0:
                saved += 1
        except Exception as e:
            print(f"  [数据库错误] {city_name} {date}: {e}")
    return saved


def get_already_crawled(conn, target_date):
    """查询指定日期已爬取的城市列表"""
    c = conn.cursor()
    c.execute("SELECT area_id FROM weather_daily WHERE date = ?", (target_date,))
    return set(row[0] for row in c.fetchall())


# ============================================================
# 主爬取逻辑
# ============================================================

def crawl_date(target_date_str, cities=None):
    """
    爬取某一天全国所有城市的天气数据。
    如果 cities 为 None，自动获取城市列表。
    """
    target_date = datetime.strptime(target_date_str, "%Y-%m-%d")
    year = target_date.year
    month = target_date.month

    if cities is None:
        cities = fetch_city_list()

    conn = sqlite3.connect(DB_PATH)

    # 检查已爬取的城市（避免重复）
    already = get_already_crawled(conn, target_date_str)
    cities_to_crawl = [(a, n, p) for a, n, p in cities if a not in already]

    if not cities_to_crawl:
        print(f"[{target_date_str}] 所有城市已爬取完毕")
        conn.close()
        return {"total": len(cities), "success": len(already), "failed": 0}

    print(f"[{target_date_str}] 需要爬取 {len(cities_to_crawl)} 个城市 "
          f"（已完成 {len(already)}，总计 {len(cities)}）")

    success_count = len(already)
    failed_count = 0

    for i, (area_id, city_name, province) in enumerate(cities_to_crawl):
        # 进度显示
        if (i + 1) % 50 == 0 or i == 0:
            print(f"  进度: {i+1}/{len(cities_to_crawl)} - 正在爬取 {city_name}({province})...")

        records = fetch_city_month(area_id, year, month)

        if records is None:
            failed_count += 1
            print(f"  [失败] {city_name}({province}) area_id={area_id}")
        elif records:
            # 只保存目标日期的数据（API 返回整月数据）
            day_records = [r for r in records if r[0] == target_date_str]
            if day_records:
                saved = save_weather_data(conn, area_id, city_name, province, day_records)
                success_count += (1 if saved > 0 else 0)
            else:
                # 该城市该日期可能还没有数据（未来日期或数据缺失）
                failed_count += 1

        # 请求间隔
        time.sleep(REQUEST_DELAY)

    conn.commit()
    conn.close()

    result = {"total": len(cities), "success": success_count, "failed": failed_count}
    print(f"[{target_date_str}] 完成！成功 {success_count}，失败 {failed_count}，总计 {len(cities)}")
    return result


def backfill(start_date_str, end_date_str=None):
    """
    回填历史数据：从 start_date 到 end_date（默认昨天），逐天爬取。
    """
    if end_date_str is None:
        # 默认到昨天
        end_date = datetime.now() - timedelta(days=1)
        end_date_str = end_date.strftime("%Y-%m-%d")

    start = datetime.strptime(start_date_str, "%Y-%m-%d")
    end = datetime.strptime(end_date_str, "%Y-%m-%d")

    if start > end:
        print("错误：开始日期不能晚于结束日期")
        return

    # 先获取城市列表（只获取一次）
    cities = fetch_city_list()

    total_days = (end - start).days + 1
    print(f"\n========== 开始回填 ==========")
    print(f"日期范围: {start_date_str} ~ {end_date_str}（共 {total_days} 天）")
    print(f"城市数量: {len(cities)}")
    print(f"预计耗时: 约 {total_days * len(cities) * REQUEST_DELAY / 60:.0f} 分钟\n")

    current = start
    day_index = 1
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        print(f"\n===== 第 {day_index}/{total_days} 天: {date_str} =====")

        result = crawl_date(date_str, cities=cities)

        # 记录日志
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            INSERT INTO crawl_log (target_date, cities_total, cities_success, cities_failed, status, started_at, finished_at)
            VALUES (?, ?, ?, ?, 'completed', datetime('now', 'localtime'), datetime('now', 'localtime'))
        """, (date_str, result["total"], result["success"], result["failed"]))
        conn.commit()
        conn.close()

        current += timedelta(days=1)
        day_index += 1

    print(f"\n========== 回填完成 ==========")
    print(f"共处理 {total_days} 天，数据库: {DB_PATH}")


# ============================================================
# 入口
# ============================================================

def main():
    init_db()

    if len(sys.argv) > 1:
        arg = sys.argv[1]

        if arg == "--backfill":
            # 回填模式
            start = BACKFILL_START
            end = None
            if len(sys.argv) > 2:
                start = sys.argv[2]
            if len(sys.argv) > 3:
                end = sys.argv[3]
            backfill(start, end)

        elif arg == "--date" and len(sys.argv) > 2:
            # 指定日期模式
            date_str = sys.argv[2]
            print(f"爬取日期: {date_str}")
            result = crawl_date(date_str)

        elif arg == "--help" or arg == "-h":
            print(__doc__)

        else:
            print(f"未知参数: {arg}")
            print("用法: python3 weather_crawler.py [--backfill [起始日期] [结束日期]] [--date YYYY-MM-DD]")

    else:
        # 默认 T+1 模式：爬昨天
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        print(f"T+1 模式：爬取昨天 ({yesterday}) 的天气数据")
        result = crawl_date(yesterday)


if __name__ == "__main__":
    main()
