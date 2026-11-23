# generated: true
"""Kindle Paperwhite 3 (1072x1448 grayscale) Weather & Daily Schedule Renderer + HTTP Server.

- Zero web-browser / Chromium dependency: pure Python stdlib + Pillow (~25-30 MB RAM)
- Domestic weather priority (UAPI by adcode) + Open-Meteo fallback/multi-day completion + disk cache
- Handles Chinese relative report_time strings (e.g. "7 分钟前发布") as well as ISO timestamps
- Token-protected HTTP endpoint (returns 404 without valid X-Wall-Token; works with dynamic home IP)
- Fully configurable via environment variables (no personal info hardcoded)
"""
from __future__ import annotations

import argparse
import hmac
import io
import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1072
HEIGHT = 1448
PAD_X = int(WIDTH * 0.03)  # 32 px
INNER_W = WIDTH - 2 * PAD_X

BJT = timezone(timedelta(hours=8))
WEEKDAYS = "日一二三四五六"

# Configurable via environment variables (generic defaults for public template)
CITY_TITLE = os.environ.get("CITY_TITLE", "北京 · 朝阳")
ADCODE = os.environ.get("ADCODE", "110105")
LATITUDE = os.environ.get("LATITUDE", "39.92")
LONGITUDE = os.environ.get("LONGITUDE", "116.44")
SCHEDULE_TITLE = os.environ.get("SCHEDULE_TITLE", "今日日程")
NO_SCHEDULE_TEXT = os.environ.get("NO_SCHEDULE_TEXT", "今日无固定日程")
# Comma-separated 7 items from Sunday (index 0) to Saturday (index 6)
_DEFAULT_SCHEDULE = ",19:00,19:00,19:30,19:00,19:00,"


def load_weekly_schedule() -> list[str]:
    raw = os.environ.get("WEEKLY_SCHEDULE", _DEFAULT_SCHEDULE)
    parts = [p.strip() for p in raw.split(",")]
    while len(parts) < 7:
        parts.append("")
    return parts[:7]


CLASSES = load_weekly_schedule()

WEATHER_LABELS = {
    0: "晴",
    1: "大致晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "冻雾",
    51: "小毛雨",
    53: "毛毛雨",
    55: "较强毛毛雨",
    56: "冻毛雨",
    57: "冻毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷雨伴冰雹",
    99: "雷雨伴冰雹",
}


def find_fonts(regular_override: str | None = None, bold_override: str | None = None) -> tuple[str, str]:
    win_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    reg_candidates = [
        regular_override,
        str(win_fonts / "msyh.ttc"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    bold_candidates = [
        bold_override,
        str(win_fonts / "msyhbd.ttc"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    reg = next((p for p in reg_candidates if p and Path(p).is_file()), None)
    bold = next((p for p in bold_candidates if p and Path(p).is_file()), reg)
    if not reg or not bold:
        raise FileNotFoundError(
            "未找到中文字体。Ubuntu 请安装 fonts-noto-cjk，或通过 --font-regular / --font-bold 指定。"
        )
    return reg, bold


def number_or_null(val: Any) -> float | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        num = float(s)
        return num if math.isfinite(num) else None
    except ValueError:
        return None


def sky_code(label: str) -> int:
    if not label:
        return -1
    if re.search(r"雷", label):
        return 95
    if re.search(r"雪", label):
        return 73
    if re.search(r"雨", label):
        return 63
    if re.search(r"雾|霾|沙|尘", label):
        return 45
    if re.search(r"多云", label):
        return 2
    if re.search(r"阴", label):
        return 3
    if re.search(r"晴", label):
        return 0
    return -1


def weather_label(code: int) -> str:
    return WEATHER_LABELS.get(code, "天气未知")


def temp_str(val: float | None) -> str:
    return f"{round(val)}°" if isinstance(val, (int, float)) else "—"


def normalize_report_time(raw_time: str, now: datetime) -> str:
    s = str(raw_time or "").strip()
    m_iso = re.match(r"^(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d)", s)
    if m_iso:
        return f"{m_iso.group(1)} {m_iso.group(2)}"
    m_min = re.search(r"(\d+)\s*分钟前", s)
    if m_min:
        return (now - timedelta(minutes=int(m_min.group(1)))).strftime("%Y-%m-%d %H:%M")
    m_hr = re.search(r"(\d+)\s*小时前", s)
    if m_hr:
        return (now - timedelta(hours=int(m_hr.group(1)))).strftime("%Y-%m-%d %H:%M")
    if "刚刚" in s:
        return now.strftime("%Y-%m-%d %H:%M")
    m_hm = re.search(r"(\d{1,2}:\d\d)", s)
    if m_hm:
        hh, mm = m_hm.group(1).split(":")
        return f"{now.strftime('%Y-%m-%d')} {int(hh):02d}:{mm}"
    return s


def format_stamp_short(stamp: str) -> str:
    s = str(stamp or "").strip()
    if re.match(r"^\d{4}-\d\d-\d\d[T ]\d\d:\d\d", s):
        return s[5:16].replace("T", " ")
    return s


def from_domestic(raw: dict[str, Any], today: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(BJT)
    value = number_or_null(raw.get("temperature"))
    if (
        str(raw.get("adcode")) != ADCODE
        or value is None
        or not raw.get("report_time")
        or not raw.get("weather")
    ):
        raise ValueError("Invalid domestic payload")

    norm_time = normalize_report_time(str(raw["report_time"]), now)
    daily: dict[str, list[Any]] = {
        "time": [],
        "weather_code": [],
        "temperature_2m_max": [],
        "temperature_2m_min": [],
    }
    days = raw.get("forecast") or []
    if (
        not days
        and number_or_null(raw.get("temp_min")) is not None
        and number_or_null(raw.get("temp_max")) is not None
    ):
        days = [
            {
                "date": norm_time[:10] if re.match(r"^\d{4}-\d\d-\d\d", norm_time) else today,
                "temp_min": raw["temp_min"],
                "temp_max": raw["temp_max"],
                "weather_day": raw["weather"],
            }
        ]

    for f in days:
        if len(daily["time"]) >= 5:
            break
        d = str(f.get("date", ""))
        if not re.match(r"^\d{4}-\d\d-\d\d$", d) or d < today:
            continue
        daily["time"].append(d)
        daily["weather_code"].append(sky_code(str(f.get("weather_day", ""))))
        daily["temperature_2m_max"].append(number_or_null(f.get("temp_max")))
        daily["temperature_2m_min"].append(number_or_null(f.get("temp_min")))

    return {
        "source": "UAPI",
        "current": {
            "time": norm_time,
            "temperature_2m": value,
            "weather_code": sky_code(str(raw["weather"])),
            "label": str(raw["weather"]),
        },
        "daily": daily,
    }


def http_get_json(url: str, timeout: float = 9.0) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "KindleWeatherWall/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise ValueError(f"HTTP {resp.status}")
        return json.loads(resp.read().decode("utf-8"))


def fetch_weather(today: str, cache_path: Path | None = None) -> tuple[dict[str, Any], bool]:
    domestic_url = f"https://uapis.cn/api/v1/misc/weather?adcode={urllib.parse.quote(ADCODE)}"
    open_meteo_url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={urllib.parse.quote(LATITUDE)}&longitude={urllib.parse.quote(LONGITUDE)}"
        "&timezone=Asia%2FShanghai&forecast_days=5"
        "&current=temperature_2m,weather_code"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min"
    )
    urls = [domestic_url + "&forecast=true", domestic_url, open_meteo_url]
    partial: dict[str, Any] | None = None

    for idx, url in enumerate(urls):
        try:
            raw = http_get_json(url, timeout=9.0)
            data = from_domestic(raw, today) if idx < 2 else raw
            if idx < 2 and len(data["daily"]["time"]) < 5:
                partial = data
                continue
            if (
                not data.get("current")
                or not data["current"].get("time")
                or not data.get("daily")
                or not data["daily"].get("time")
            ):
                raise ValueError("Incomplete weather payload")
            if partial and idx == 2:
                data["current"] = partial["current"]
                data["source"] = "实况 UAPI / 预报 Open-Meteo"
            elif idx == 2:
                data["source"] = "Open-Meteo"

            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return data, True
        except Exception:
            continue

    if partial:
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(partial, ensure_ascii=False, indent=2), encoding="utf-8")
        return partial, True

    if cache_path and cache_path.is_file():
        return json.loads(cache_path.read_text(encoding="utf-8")), False

    raise RuntimeError("所有天气数据源不可达且无本地缓存")


def sample_weather_data(now: datetime) -> dict[str, Any]:
    days = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(5)]
    return {
        "source": "UAPI（示例数据）",
        "current": {
            "time": now.strftime("%Y-%m-%d %H:%M"),
            "temperature_2m": 11,
            "weather_code": 2,
            "label": "多云",
        },
        "daily": {
            "time": days,
            "weather_code": [2, 0, 3, 63, 2],
            "temperature_2m_min": [6, 7, 8, 5, 6],
            "temperature_2m_max": [17, 19, 15, 12, 16],
        },
    }


def _draw_thick_line(draw: ImageDraw.ImageDraw, p1: tuple[float, float], p2: tuple[float, float], w: int) -> None:
    draw.line([p1, p2], fill=0, width=w)
    r = w / 2.0
    for x, y in (p1, p2):
        draw.ellipse((x - r, y - r, x + r, y + r), fill=0)


def _draw_sun(draw: ImageDraw.ImageDraw, cx: float, cy: float, scale: float, stroke: int) -> None:
    r = 12 * scale
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255, outline=0, width=stroke)
    ray_inner = 18 * scale
    ray_outer = 25 * scale
    for deg in range(0, 360, 45):
        rad = math.radians(deg)
        p1 = (cx + math.cos(rad) * ray_inner, cy + math.sin(rad) * ray_inner)
        p2 = (cx + math.cos(rad) * ray_outer, cy + math.sin(rad) * ray_outer)
        _draw_thick_line(draw, p1, p2, stroke)


def _sample_cubic(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    steps: int = 36,
) -> list[tuple[float, float]]:
    pts = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1.0 - t
        x = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
        y = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
        pts.append((x, y))
    return pts


def _draw_cloud(draw: ImageDraw.ImageDraw, ox: float, oy: float, scale: float, stroke: int) -> None:
    raw_pts = (
        _sample_cubic((15, 43), (2, 43), (3, 27), (15, 26))[:-1]
        + _sample_cubic((15, 26), (17, 12), (37, 10), (42, 23))[:-1]
        + _sample_cubic((42, 23), (58, 20), (63, 42), (48, 43))[:-1]
        + [(48, 43), (15, 43)]
    )
    pts = [(ox + x * scale, oy + y * scale) for x, y in raw_pts]
    draw.polygon(pts, fill=255)
    r = stroke / 2.0
    for a, b in zip(pts, pts[1:]):
        draw.line([a, b], fill=0, width=stroke)
    for x, y in pts:
        draw.ellipse((x - r, y - r, x + r, y + r), fill=0)


def render_weather_icon(code: int, side: int, is_hero: bool) -> Image.Image:
    ss = 2
    canvas_side = side * ss
    img = Image.new("L", (canvas_side, canvas_side), 255)
    draw = ImageDraw.Draw(img)
    scale = canvas_side / 64.0
    stroke = max(2, int(round((4.8 if is_hero else 3.8) * scale)))

    if code == 0:
        _draw_sun(draw, 32 * scale, 30 * scale, scale, stroke)
    elif code in (1, 2):
        _draw_sun(draw, 41 * scale, 20 * scale, scale * 0.72, max(2, int(stroke * 0.85)))
        _draw_cloud(draw, 0, 2 * scale, scale, stroke)
    elif code in (45, 48):
        _draw_cloud(draw, 0, -2 * scale, scale, stroke)
        _draw_thick_line(draw, (12 * scale, 50 * scale), (52 * scale, 50 * scale), stroke)
        _draw_thick_line(draw, (17 * scale, 57 * scale), (46 * scale, 57 * scale), stroke)
    elif code >= 95:
        _draw_cloud(draw, 0, -2 * scale, scale, stroke)
        pts = [(33 * scale, 41 * scale), (25 * scale, 51 * scale), (34 * scale, 51 * scale), (28 * scale, 60 * scale)]
        for a, b in zip(pts, pts[1:]):
            _draw_thick_line(draw, a, b, stroke)
    elif (71 <= code <= 77) or code in (85, 86):
        _draw_cloud(draw, 0, -2 * scale, scale, stroke)
        r = 3.0 * scale
        for x, y in ((20, 52), (32, 56), (44, 51)):
            draw.ellipse((x * scale - r, y * scale - r, x * scale + r, y * scale + r), fill=0)
    elif 51 <= code <= 82:
        _draw_cloud(draw, 0, -2 * scale, scale, stroke)
        for x0 in (21, 33, 45):
            _draw_thick_line(draw, (x0 * scale, 49 * scale), ((x0 - 3) * scale, 57 * scale), stroke)
    elif code == 3:
        _draw_cloud(draw, 0, 2 * scale, scale, stroke)

    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    return img.resize((side, side), resample)


def fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: str,
    max_size: int,
    max_width: int,
    min_size: int = 20,
) -> ImageFont.FreeTypeFont:
    size = max_size
    while size >= min_size:
        f = ImageFont.truetype(font_path, size)
        bbox = draw.textbbox((0, 0), text, font=f)
        if (bbox[2] - bbox[0]) <= max_width:
            return f
        size -= 2
    return ImageFont.truetype(font_path, min_size)


def draw_centered_text(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: int = 0,
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text((cx - tw / 2 - bbox[0], cy - th / 2 - bbox[1]), text, font=font, fill=fill)


def render_dashboard_png(
    data: dict[str, Any],
    now: datetime,
    is_fresh: bool,
    font_regular: str,
    font_bold: str,
) -> bytes:
    img = Image.new("L", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(img)

    today = now.strftime("%Y-%m-%d")
    wday = (now.weekday() + 1) % 7

    y_head = 0
    h_head = int(HEIGHT * 0.085)
    y_hero = y_head + h_head
    h_hero = int(HEIGHT * 0.31)
    y_range = y_hero + h_hero
    h_range = int(HEIGHT * 0.08)
    y_days = y_range + h_range
    h_days = int(HEIGHT * 0.245)
    y_course = y_days + h_days
    h_course = int(HEIGHT * 0.215)
    y_foot = y_course + h_course
    h_foot = HEIGHT - y_foot

    # 1. Header
    f_city = ImageFont.truetype(font_bold, 74)
    f_date = ImageFont.truetype(font_regular, 60)
    date_text = f"{now.year}年{now.month}月{now.day}日"

    city_bb = draw.textbbox((0, 0), CITY_TITLE, font=f_city)
    city_h = city_bb[3] - city_bb[1]
    draw.text((PAD_X - city_bb[0], y_head + (h_head - city_h) / 2 - city_bb[1]), CITY_TITLE, font=f_city, fill=0)

    date_bb = draw.textbbox((0, 0), date_text, font=f_date)
    date_w = date_bb[2] - date_bb[0]
    date_h = date_bb[3] - date_bb[1]
    draw.text(
        (WIDTH - PAD_X - date_w - date_bb[0], y_head + (h_head - date_h) / 2 - date_bb[1]),
        date_text,
        font=f_date,
        fill=0,
    )
    draw.rectangle((PAD_X, y_hero - 3, WIDTH - PAD_X, y_hero), fill=0)

    # 2. Hero
    cur = data["current"]
    stamp = str(cur["time"])
    cur_temp = cur.get("temperature_2m")
    cur_code = int(cur.get("weather_code", -1))
    cur_label = str(cur.get("label") or weather_label(cur_code))

    icon_side = int(min(WIDTH * 0.36, HEIGHT * 0.28))
    hero_icon = render_weather_icon(cur_code, icon_side, is_hero=True)
    left_w = int(INNER_W * 0.44)
    img.paste(hero_icon, (PAD_X + (left_w - icon_side) // 2, y_hero + (h_hero - icon_side) // 2))

    right_col_x0 = PAD_X + left_w
    right_col_w = INNER_W - left_w
    right_edge = WIDTH - PAD_X - int(right_col_w * 0.10)

    has_iso_date = bool(re.match(r"^\d{4}-\d\d-\d\d", stamp))
    stale = (not is_fresh) or (has_iso_date and stamp[:10] != today)
    top_label = f"上次实况 · {format_stamp_short(stamp)}" if stale else "当前气温"
    f_cur_label = fit_font(draw, top_label, font_regular, 46, right_edge - right_col_x0)

    temp_num = str(round(cur_temp)) if isinstance(cur_temp, (int, float)) else "—"
    num_size = 205 if len(temp_num) > 2 else 255
    f_temp_num = ImageFont.truetype(font_bold, num_size)
    f_temp_unit = ImageFont.truetype(font_bold, int(num_size * 0.42))
    f_cond = fit_font(draw, cur_label, font_regular, 76, right_edge - right_col_x0)

    lbl_bb = draw.textbbox((0, 0), top_label, font=f_cur_label)
    num_bb = draw.textbbox((0, 0), temp_num, font=f_temp_num)
    unit_bb = draw.textbbox((0, 0), "°C", font=f_temp_unit)
    cond_bb = draw.textbbox((0, 0), cur_label, font=f_cond)

    lbl_h = lbl_bb[3] - lbl_bb[1]
    num_h = num_bb[3] - num_bb[1]
    cond_h = cond_bb[3] - cond_bb[1]
    gap1, gap2 = 22, 24
    by = y_hero + (h_hero - (lbl_h + gap1 + num_h + gap2 + cond_h)) / 2

    draw.text((right_edge - (lbl_bb[2] - lbl_bb[0]) - lbl_bb[0], by - lbl_bb[1]), top_label, font=f_cur_label, fill=0)

    num_y = by + lbl_h + gap1
    unit_w = unit_bb[2] - unit_bb[0]
    num_w = num_bb[2] - num_bb[0]
    unit_x = right_edge - unit_w
    num_x = unit_x - 6 - num_w
    draw.text((num_x - num_bb[0], num_y - num_bb[1]), temp_num, font=f_temp_num, fill=0)
    draw.text((unit_x - unit_bb[0], num_y + int(num_h * 0.04) - unit_bb[1]), "°C", font=f_temp_unit, fill=0)

    cond_y = num_y + num_h + gap2
    draw.text((right_edge - (cond_bb[2] - cond_bb[0]) - cond_bb[0], cond_y - cond_bb[1]), cur_label, font=f_cond, fill=0)

    # 3. Today's min/max range
    daily = data["daily"]
    range_text = "暂无今日温度范围"
    future_days: list[tuple[str, int, float | None, float | None]] = []
    for i, d_str in enumerate(daily.get("time", [])):
        t_min = daily["temperature_2m_min"][i] if i < len(daily["temperature_2m_min"]) else None
        t_max = daily["temperature_2m_max"][i] if i < len(daily["temperature_2m_max"]) else None
        w_code = int(daily["weather_code"][i]) if i < len(daily["weather_code"]) else -1
        if d_str == today:
            range_text = f"今日最低 {temp_str(t_min)}   /   最高 {temp_str(t_max)}"
        elif d_str > today and len(future_days) < 4:
            future_days.append((d_str, w_code, t_min, t_max))

    f_range = fit_font(draw, range_text, font_bold, 66, INNER_W)
    r_bb = draw.textbbox((0, 0), range_text, font=f_range)
    draw.text((PAD_X - r_bb[0], y_range + (h_range - (r_bb[3] - r_bb[1])) / 2 - r_bb[1]), range_text, font=f_range, fill=0)
    draw.rectangle((PAD_X, y_days - 3, WIDTH - PAD_X, y_days), fill=0)

    # 4. Future 4 days
    if future_days:
        cols = len(future_days)
        col_w = INNER_W / cols
        small_side = int(min(WIDTH * 0.15, HEIGHT * 0.115))
        for idx, (d_str, w_code, t_min, t_max) in enumerate(future_days):
            cx = PAD_X + col_w * (idx + 0.5)
            if idx > 0:
                div_x = int(round(PAD_X + col_w * idx))
                draw.line((div_x, y_days + 14, div_x, y_course - 14), fill=136, width=2)

            dt = datetime.strptime(d_str, "%Y-%m-%d")
            day_wday = (dt.weekday() + 1) % 7
            day_title = f"周{WEEKDAYS[day_wday]}"
            cond_txt = weather_label(w_code)
            rng_txt = f"{temp_str(t_min)}/{temp_str(t_max)}"

            draw_centered_text(draw, cx, y_days + 46, day_title, ImageFont.truetype(font_bold, 58), fill=0)
            s_icon = render_weather_icon(w_code, small_side, is_hero=False)
            img.paste(s_icon, (int(round(cx - small_side / 2)), y_days + 78))
            draw_centered_text(draw, cx, y_days + 258, cond_txt, fit_font(draw, cond_txt, font_regular, 46, int(col_w - 18)), fill=0)
            draw_centered_text(draw, cx, y_days + 314, rng_txt, fit_font(draw, rng_txt, font_bold, 48, int(col_w - 14)), fill=0)
    else:
        draw_centered_text(draw, WIDTH / 2, y_days + h_days / 2, "多日预报暂未返回", ImageFont.truetype(font_regular, 48), fill=0)

    # 5. Weekday & daily schedule (single line, never wraps)
    draw.rectangle((PAD_X, y_course, WIDTH - PAD_X, y_course + 3), fill=0)
    class_time = CLASSES[wday]
    line1 = f"今天是星期{WEEKDAYS[wday]}"
    line2 = f"{SCHEDULE_TITLE}  {class_time}" if class_time else NO_SCHEDULE_TEXT

    f_wday = ImageFont.truetype(font_regular, 66)
    f_lesson = fit_font(draw, line2, font_bold, 92, INNER_W)
    bb1 = draw.textbbox((0, 0), line1, font=f_wday)
    bb2 = draw.textbbox((0, 0), line2, font=f_lesson)
    h1 = bb1[3] - bb1[1]
    h2 = bb2[3] - bb2[1]
    cy = y_course + (h_course - (h1 + 34 + h2)) / 2
    draw.text((PAD_X - bb1[0], cy - bb1[1]), line1, font=f_wday, fill=0)
    draw.text((PAD_X - bb2[0], cy + h1 + 34 - bb2[1]), line2, font=f_lesson, fill=0)

    # 6. Footer
    draw.line((PAD_X, y_foot, WIDTH - PAD_X, y_foot), fill=153, width=2)
    src_name = str(data.get("source") or "Open-Meteo")
    stamp_short = format_stamp_short(stamp)
    fetched_short = now.strftime("%m-%d %H:%M")
    if is_fresh:
        foot_text = f"{src_name} · 实况 {stamp_short} · 本机取数 {fetched_short}"
    else:
        foot_text = f"缓存天气 · {src_name} · 实况 {stamp_short} · 本机取数 {fetched_short}"

    f_foot = fit_font(draw, foot_text, font_regular, 34, INNER_W)
    fbb = draw.textbbox((0, 0), foot_text, font=f_foot)
    draw.text((PAD_X - fbb[0], y_foot + (h_foot - (fbb[3] - fbb[1])) / 2 - fbb[1]), foot_text, font=f_foot, fill=68)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class WeatherWallState:
    def __init__(self, cache_path: Path, font_regular: str, font_bold: str, token: str):
        self.cache_path = cache_path
        self.font_regular = font_regular
        self.font_bold = font_bold
        self.token = token
        self.lock = threading.Lock()
        self.last_fetch_mono = 0.0
        self.last_data: dict[str, Any] | None = None
        self.last_fresh = False

    def get_frame(self, force_refresh: bool = False) -> bytes:
        now = datetime.now(BJT)
        today = now.strftime("%Y-%m-%d")
        with self.lock:
            if force_refresh or self.last_data is None or (time.monotonic() - self.last_fetch_mono) >= 60:
                try:
                    data, fresh = fetch_weather(today, self.cache_path)
                    self.last_data = data
                    self.last_fresh = fresh
                    self.last_fetch_mono = time.monotonic()
                except Exception:
                    if self.last_data is None:
                        raise
                    self.last_fresh = False
            data = self.last_data
            fresh = self.last_fresh
        return render_dashboard_png(data, now, fresh, self.font_regular, self.font_bold)


def make_handler(state: WeatherWallState):
    class Handler(BaseHTTPRequestHandler):
        def _authorized(self, query: dict[str, list[str]]) -> bool:
            if not state.token:
                return False
            hdr = self.headers.get("X-Wall-Token", "")
            qtok = (query.get("token") or [""])[0]
            cand = hdr or qtok
            return bool(cand) and hmac.compare_digest(cand, state.token)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if not self._authorized(query):
                self.send_response(404)
                self.end_headers()
                return

            if parsed.path == "/health":
                body = json.dumps({"ok": True, "service": "kindle-wall"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/wall.png":
                try:
                    png = state.get_frame(force_refresh=(query.get("force") == ["1"]))
                except Exception as exc:
                    msg = f"render failed: {exc}".encode("utf-8")
                    self.send_response(503)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    self.wfile.write(msg)
                    return

                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(png)))
                self.end_headers()
                self.wfile.write(png)
                return

            self.send_response(404)
            self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:
            ts = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
            safe_args = tuple(re.sub(r"token=[^&\s]+", "token=***", str(a)) for a in args)
            print(f"[{ts}] {self.address_string()} " + (fmt % safe_args), flush=True)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Kindle PW3 Weather Wall Renderer & Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--token", default=os.environ.get("WALL_TOKEN", ""))
    parser.add_argument("--cache", type=Path, default=Path(__file__).with_name("last_weather.json"))
    parser.add_argument("--font-regular", default=None)
    parser.add_argument("--font-bold", default=None)
    parser.add_argument("--preview", type=Path, help="Render one PNG locally and exit")
    parser.add_argument("--sample", action="store_true", help="Use offline sample data for preview")
    args = parser.parse_args()

    font_reg, font_bold = find_fonts(args.font_regular, args.font_bold)

    if args.preview:
        now = datetime.now(BJT)
        if args.sample:
            data, fresh = sample_weather_data(now), True
        else:
            data, fresh = fetch_weather(now.strftime("%Y-%m-%d"), args.cache)
        png = render_dashboard_png(data, now, fresh, font_reg, font_bold)
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        args.preview.write_bytes(png)
        print(f"Rendered {args.preview} ({len(png)} bytes, fresh={fresh})")
        return

    if not args.token or len(args.token) < 16:
        raise SystemExit("ERROR: 请通过环境变量 WALL_TOKEN 或 --token 提供至少 16 位随机访问令牌。")

    state = WeatherWallState(args.cache, font_reg, font_bold, args.token)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    print(f"Kindle Weather Wall listening on {args.host}:{args.port} (token auth enabled)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
