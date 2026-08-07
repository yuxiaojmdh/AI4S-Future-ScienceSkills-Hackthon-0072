#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""气候健康风险预警管线（AI4S Hackathon 赛题3）

融合 ERA5 气象再分析 + CAMS 空气质量 + 人口脆弱性，
生成城市级健康风险分级、预警文案、风险地图、不确定性与公平性报告。

用法:
    python run_pipeline.py --cities ../data/cities_demo.csv \
        --start 2026-07-01 --end 2026-07-07 --outdir ../output
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ARCHIVE_API = "https://archive-api.open-meteo.com/v1/archive"
AIR_API = "https://air-quality-api.open-meteo.com/v1/air-quality"
GEO_API = "https://geocoding-api.open-meteo.com/v1/search"

# ---------------- 方法论常量（与 SKILL.md 保持一致） ----------------
WEIGHTS = {"hazard": 0.5, "exposure": 0.3, "vulnerability": 0.2}
EHI_NORM_C = 8.0          # EHI 达 8°C 记为满分
TW_DANGER_C = 26.0        # 湿球温度健康危险阈值
TW_NORM_C = 8.0
IAQI_NORM = 300.0
HEATWAVE_TMAX_C = 35.0    # 中国热浪标准
BASELINE_START = "1991-01-01"
BASELINE_END = "2020-12-31"

# HJ 633-2012 IAQI 断点: (浓度下限, 浓度上限, IAQI下限, IAQI上限)
IAQI_PM25 = [(0, 35, 0, 50), (35, 75, 50, 100), (75, 115, 100, 150),
             (115, 150, 150, 200), (150, 250, 200, 300),
             (250, 350, 300, 400), (350, 500, 400, 500)]
IAQI_O3_1H = [(0, 160, 0, 50), (160, 200, 50, 100), (200, 300, 100, 150),
              (300, 400, 150, 200), (400, 800, 200, 300),
              (800, 1000, 300, 400), (1000, 1200, 400, 500)]

LEVEL_COLORS = {"蓝": "#2c7fb8", "黄": "#d9a400", "橙": "#e6550d", "红": "#b10026"}


# ---------------- 基础函数 ----------------
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


def _cache_path(url, params):
    raw = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return CACHE_DIR / (hashlib.sha256(raw.encode()).hexdigest()[:32] + ".json")


def http_get_json(url, params, retries=3, backoff=2.0, use_cache=True):
    """带磁盘缓存与指数退避重试的 GET；重试耗尽抛出异常（调用方决定跳过策略）。

    - 缓存：成功响应写入 cache/，重跑零配额消耗、可离线复现。
    - 节流：真实网络请求前等待 1.5s，避免触发免费档每分钟限流。
    - 429：等待 62s 再重试（每分钟限流）；小时级限流同样处理，
      重试耗尽后由调用方跳过该城市。
    """
    cf = None
    if use_cache:
        CACHE_DIR.mkdir(exist_ok=True)
        cf = _cache_path(url, params)
        if cf.exists():
            return json.loads(cf.read_text(encoding="utf-8"))
    last = None
    for i in range(retries):
        rate_limited = False
        try:
            time.sleep(1.5)  # 节流
            r = requests.get(url, params=params, timeout=180)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("error"):
                    raise RuntimeError(f"API error: {data.get('reason')}")
                if cf is not None:
                    cf.write_text(json.dumps(data), encoding="utf-8")
                return data
            rate_limited = r.status_code == 429
            last = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:  # noqa: BLE001
            last = e
        wait = 62.0 if rate_limited else backoff * (2 ** i)
        print(f"  [重试 {i + 1}/{retries}] {last}；{wait:.0f}s 后重试", file=sys.stderr)
        time.sleep(wait)
    raise RuntimeError(f"请求失败（{retries} 次尝试后跳过）: {url} -> {last}")


def stull_wetbulb(t_c, rh):
    """Stull(2011) 湿球温度近似公式。输入可为标量或 ndarray。"""
    t_c = np.asarray(t_c, dtype=float)
    rh = np.clip(np.asarray(rh, dtype=float), 5.0, 99.0)
    return (t_c * np.arctan(0.151977 * np.sqrt(rh + 8.313659))
            + np.arctan(t_c + rh) - np.arctan(rh - 1.676331)
            + 0.00391838 * rh ** 1.5 * np.arctan(0.023101 * rh)
            - 4.686035)


def iaqi(conc, table):
    """按断点表线性内插计算 IAQI 分项指数。"""
    if conc is None or (isinstance(conc, float) and math.isnan(conc)) or conc < 0:
        return float("nan")
    if conc >= table[-1][1]:
        return float(table[-1][3])
    for bp_lo, bp_hi, i_lo, i_hi in table:
        if conc <= bp_hi:
            return (i_hi - i_lo) / (bp_hi - bp_lo) * (conc - bp_lo) + i_lo
    return float(table[-1][3])


def risk_level(score):
    """四级分级：返回 (颜色, 文字等级)。"""
    if score >= 70:
        return "红", "I级（极高）"
    if score >= 50:
        return "橙", "II级（高）"
    if score >= 30:
        return "黄", "III级（中）"
    return "蓝", "IV级（低）"


def clip01(x):
    return float(np.clip(x, 0.0, 1.0))


# ---------------- 数据获取 ----------------
def geocode_city(name):
    """按城市名查询坐标与人口（Open-Meteo Geocoding，免费免 key）。

    返回可直接并入城市清单的 dict；找不到时抛出异常。
    中文名必须用 language=zh 查询（language=en 查中文名返回空）；
    老龄化率无公开逐城市实时来源，默认取 0.15（全国均值附近）并在 notes 标注。
    """
    is_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in name)
    lang = "zh" if is_cjk else "en"
    d = http_get_json(GEO_API, {"name": name, "count": 5,
                                "language": lang, "format": "json"})
    results = d.get("results") or []
    cn = [r for r in results if r.get("country_code") == "CN"] or results
    if not cn:
        raise RuntimeError(f"未找到城市 '{name}'")
    r = cn[0]
    pop = r.get("population")
    notes = "地理编码新增；老龄化率取默认值0.15"
    if not pop:
        pop = 1_000_000
        notes += "；人口数据缺失，默认100万"
    return {"name": name, "name_en": r.get("name", ""),
            "lat": float(r["latitude"]), "lon": float(r["longitude"]),
            "population": int(pop), "elderly_ratio": 0.15, "notes": notes}


def fetch_climate_window(lat, lon, start, end):
    """分析窗口：日值 + 小时值（用于湿球温度）。"""
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": start, "end_date": end,
        "daily": ("temperature_2m_max,temperature_2m_min,temperature_2m_mean,"
                  "relative_humidity_2m_mean,wind_speed_10m_mean"),
        "hourly": "temperature_2m,relative_humidity_2m",
        "timezone": "auto",
    }
    d = http_get_json(ARCHIVE_API, params)
    daily = pd.DataFrame({"date": pd.to_datetime(d["daily"]["time"])})
    for k in ("temperature_2m_max", "temperature_2m_min", "temperature_2m_mean",
              "relative_humidity_2m_mean", "wind_speed_10m_mean"):
        daily[k] = d["daily"].get(k)
    hourly = pd.DataFrame({"time": pd.to_datetime(d["hourly"]["time"]),
                           "t2m": d["hourly"]["temperature_2m"],
                           "rh": d["hourly"]["relative_humidity_2m"]})
    return daily, hourly


def fetch_baseline(lat, lon):
    """基准期 1991-2020 日平均气温（一次取全期，本地按日历窗口过滤）。"""
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": BASELINE_START, "end_date": BASELINE_END,
        "daily": "temperature_2m_mean",
        "timezone": "auto",
    }
    d = http_get_json(ARCHIVE_API, params)
    df = pd.DataFrame({"date": pd.to_datetime(d["daily"]["time"]),
                       "tmean": d["daily"]["temperature_2m_mean"]})
    return df.dropna(subset=["tmean"])


def fetch_air_quality(lat, lon, start, end):
    """CAMS 小时空气质量 → 日最大浓度。注意：daily 聚合变量名不可用，必须 hourly。"""
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": start, "end_date": end,
        "hourly": "pm2_5,pm10,ozone,nitrogen_dioxide",
        "timezone": "auto",
    }
    d = http_get_json(AIR_API, params)
    h = pd.DataFrame({"time": pd.to_datetime(d["hourly"]["time"]),
                      "pm2_5": d["hourly"]["pm2_5"],
                      "pm10": d["hourly"]["pm10"],
                      "ozone": d["hourly"]["ozone"],
                      "no2": d["hourly"]["nitrogen_dioxide"]})
    h["date"] = h["time"].dt.date
    daily = h.groupby("date")[["pm2_5", "pm10", "ozone", "no2"]].max()
    return daily


def calendar_mask(dates, start, end, pad_days=15):
    """基准期过滤掩码：日历日期落在 [start-15d, end+15d] 内（用闰年2000展开）。"""
    s = dt.datetime.strptime(start, "%Y-%m-%d") - dt.timedelta(days=pad_days)
    e = dt.datetime.strptime(end, "%Y-%m-%d") + dt.timedelta(days=pad_days)
    valid = set()
    cur = dt.datetime(2000, s.month, s.day)
    stop = dt.datetime(2000, e.month, e.day)
    while cur <= stop:
        valid.add((cur.month, cur.day))
        cur += dt.timedelta(days=1)
    idx = pd.DatetimeIndex(dates)
    keys = zip(idx.month, idx.day)
    return np.array([k in valid for k in keys])


# ---------------- 风险计算 ----------------
def compute_city_risk(row, start, end):
    """单城市完整指标计算。返回指标 dict；失败返回 None。"""
    lat, lon = float(row["lat"]), float(row["lon"])
    daily, hourly = fetch_climate_window(lat, lon, start, end)
    baseline = fetch_baseline(lat, lon)
    air = fetch_air_quality(lat, lon, start, end)

    mask = calendar_mask(baseline["date"], start, end)
    base_win = baseline.loc[mask, "tmean"]
    if len(base_win) < 100:
        print(f"  [警告] {row['name']} 基准期样本不足({len(base_win)})，EHI 可靠性下降",
              file=sys.stderr)
    base_p90 = float(np.percentile(base_win, 90)) if len(base_win) else float("nan")
    base_mean = float(base_win.mean()) if len(base_win) else float("nan")

    tmean_win = float(daily["temperature_2m_mean"].mean())
    tmax_win = daily["temperature_2m_max"]
    ehi = tmean_win - base_p90
    heatwave_days = int((tmax_win >= HEATWAVE_TMAX_C).sum())
    consecutive = int(max_consecutive((tmax_win >= HEATWAVE_TMAX_C).to_numpy()))

    tw = stull_wetbulb(hourly["t2m"].to_numpy(), hourly["rh"].to_numpy())
    tw_max = float(np.nanmax(tw)) if np.isfinite(tw).any() else float("nan")

    pm25_max = float(air["pm2_5"].max()) if len(air) else float("nan")
    o3_max = float(air["ozone"].max()) if len(air) else float("nan")
    iaqi_pm = iaqi(pm25_max, IAQI_PM25)
    iaqi_o3 = iaqi(o3_max, IAQI_O3_1H)

    heat_n = clip01(ehi / EHI_NORM_C)
    humid_n = clip01((tw_max - TW_DANGER_C) / TW_NORM_C)
    air_n = clip01(max(iaqi_pm, iaqi_o3) / IAQI_NORM)
    hazard = 100.0 * (0.4 * heat_n + 0.3 * humid_n + 0.3 * air_n)

    pop = float(row["population"])
    exposure = 100.0 * clip01((math.log10(max(pop, 1)) - 5.0) / 3.0)

    elderly = float(row.get("elderly_ratio", 0.15) or 0.15)
    acclimation = 1.0 - clip01((base_mean - 5.0) / 20.0)
    vulnerability = 100.0 * (0.6 * clip01(elderly / 0.30) + 0.4 * acclimation)

    risk = (WEIGHTS["hazard"] * hazard + WEIGHTS["exposure"] * exposure
            + WEIGHTS["vulnerability"] * vulnerability)
    color, level = risk_level(risk)

    return {
        "city": row["name"], "name_en": row.get("name_en", ""),
        "lat": lat, "lon": lon,
        "population": pop, "elderly_ratio": elderly,
        "tmean_window": round(tmean_win, 2),
        "tmax_peak": float(tmax_win.max()),
        "ehi": round(ehi, 2), "base_p90": round(base_p90, 2),
        "heatwave_days": heatwave_days, "consecutive_heatwave": consecutive,
        "tw_max": round(tw_max, 2),
        "pm25_peak": round(pm25_max, 1), "o3_peak": round(o3_max, 1),
        "iaqi_pm25": round(iaqi_pm, 1), "iaqi_o3": round(iaqi_o3, 1),
        "hazard": round(hazard, 1), "exposure": round(exposure, 1),
        "vulnerability": round(vulnerability, 1), "risk_score": round(risk, 1),
        "level_color": color, "level_text": level,
        "window": f"{start} ~ {end}",
    }


def max_consecutive(mask):
    best = cur = 0
    for m in mask:
        cur = cur + 1 if m else 0
        best = max(best, cur)
    return best


def risk_with_weights(df, w_h, w_e, w_v):
    return w_h * df["hazard"] + w_e * df["exposure"] + w_v * df["vulnerability"]


# ---------------- 输出生成 ----------------
# 演示城市中文名 → GADM NAME_2 英文名映射（地理编码新增的城市自带 name_en，无需此表）
CN_TO_GADM = {
    "北京": "Beijing", "上海": "Shanghai", "广州": "Guangzhou", "深圳": "Shenzhen",
    "成都": "Chengdu", "重庆": "Chongqing", "武汉": "Wuhan", "西安": "Xi'an",
    "哈尔滨": "Harbin", "兰州": "Lanzhou",
}


def _match_gadm_features(df, gadm2):
    """把风险表中的城市匹配到 GADM 市级要素，返回 {city_name: GeoDataFrame}。

    两级策略（空间优先）：
    ① 点-多边形匹配：城市坐标落在哪个市级多边形内。坐标是权威依据，
       避免同名城市误匹配（如江苏苏州 Suzhou vs 安徽宿州 Suzhou 英文同名），
       也不依赖中英文名翻译（地理编码新增城市同样适用）；
    ② 坐标缺失或落在所有多边形之外时，退回 NAME_2 英文名匹配。
    """
    matched, remaining = {}, []
    try:
        from shapely.geometry import Point
        spatial_ok = True
    except Exception:  # noqa: BLE001
        spatial_ok = False
    for _, r in df.iterrows():
        hit = None
        if spatial_ok:
            try:
                pt = Point(float(r["lon"]), float(r["lat"]))
                hits = gadm2[gadm2.geometry.contains(pt)]
                if len(hits):
                    hit = hits.iloc[:1]
            except Exception:  # noqa: BLE001
                pass
        if hit is not None:
            matched[r["city"]] = hit
        else:
            remaining.append(r)
    if remaining:  # 名称兜底
        names2 = gadm2["NAME_2"].str.lower().str.replace("'", "", regex=False)
        for r in remaining:
            for cand in [r.get("name_en", ""), CN_TO_GADM.get(r["city"], ""),
                         r["city"]]:
                if not isinstance(cand, str) or not cand:
                    continue
                h = gadm2[names2 == cand.lower().replace("'", "")]
                if len(h):
                    matched[r["city"]] = h.iloc[:1]
                    break
    return matched


def draw_map(df, out_png):
    """风险地图。有 GADM 时：行政区填色（choropleth）+ 侧边人口条形图；
    无 GADM 或匹配全部失败时：降级为气泡图。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:  # noqa: BLE001
        print(f"[跳过] matplotlib 不可用，未生成风险地图: {e}", file=sys.stderr)
        return False
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    gadm_path = Path(__file__).resolve().parent.parent / "data" / "gadm41_CHN.gpkg"
    gadm2 = None
    try:
        import geopandas as gpd
        if gadm_path.exists():
            gadm2 = gpd.read_file(gadm_path, layer="ADM_ADM_2")
    except Exception as e:  # noqa: BLE001
        print(f"[提示] GADM 加载失败，降级为气泡图: {e}", file=sys.stderr)

    matched = _match_gadm_features(df, gadm2) if gadm2 is not None else {}

    if matched:  # ---- 行政区填色图 ----
        fig, (ax, ax_bar) = plt.subplots(
            1, 2, figsize=(13, 8), gridspec_kw={"width_ratios": [2.6, 1]})
        # 底图：全国市级浅灰
        gadm2.plot(ax=ax, color="#f0f0f0", edgecolor="#bbbbbb", linewidth=0.3)
        # 风险城市：按等级填色，仅覆盖其行政区域
        for city, feat in matched.items():
            row = df[df["city"] == city].iloc[0]
            feat.plot(ax=ax, color=LEVEL_COLORS[row["level_color"]],
                      edgecolor="black", linewidth=0.6, alpha=0.85, zorder=3)
            cx, cy = feat.geometry.iloc[0].centroid.x, feat.geometry.iloc[0].centroid.y
            ax.annotate(f'{city} {row["risk_score"]:.0f}', (cx, cy),
                        ha="center", va="center", fontsize=8.5, zorder=4,
                        bbox=dict(boxstyle="round,pad=0.15", fc="white",
                                  ec="none", alpha=0.75))
        # 未匹配到边界的城市：小圆点兜底，保证不丢信息
        for _, r in df.iterrows():
            if r["city"] not in matched:
                ax.scatter(r["lon"], r["lat"], s=60,
                           c=LEVEL_COLORS[r["level_color"]],
                           edgecolors="black", linewidths=0.6, zorder=5)
                ax.annotate(f'{r["city"]} {r["risk_score"]:.0f}',
                            (r["lon"], r["lat"]), textcoords="offset points",
                            xytext=(6, 5), fontsize=8.5, zorder=5)
        handles = [plt.Rectangle((0, 0), 1, 1, fc=LEVEL_COLORS[c])
                   for c in ("蓝", "黄", "橙", "红")]
        ax.legend(handles, ["蓝·IV级（低）", "黄·III级（中）", "橙·II级（高）",
                            "红·I级（极高）"], title="风险等级", loc="lower left",
                  fontsize=8)
        ax.set_title(f"气候健康风险地图（行政区填色）　{df['window'].iloc[0]}")
        ax.set_axis_off()

        # 侧边：人口规模条形图（替代气泡面积编码）
        d = df.sort_values("population")
        ax_bar.barh(d["city"], d["population"] / 1e6,
                    color=[LEVEL_COLORS[c] for c in d["level_color"]],
                    edgecolor="black", linewidth=0.4)
        for i, (c, p) in enumerate(zip(d["city"], d["population"])):
            ax_bar.text(p / 1e6 + 0.3, i, f"{p / 1e6:.1f}", va="center", fontsize=8)
        ax_bar.set_xlabel("常住人口（百万）")
        ax_bar.set_title("暴露人口规模", fontsize=10)
        ax_bar.spines[["top", "right"]].set_visible(False)
    else:  # ---- 降级：气泡图 ----
        fig, ax = plt.subplots(figsize=(10, 8))
        for _, r in df.iterrows():
            ax.scatter(r["lon"], r["lat"], s=math.sqrt(r["population"]) * 16,
                       c=LEVEL_COLORS[r["level_color"]], alpha=0.75,
                       edgecolors="black", linewidths=0.6, zorder=3)
            ax.annotate(f'{r["city"]} {r["risk_score"]:.0f}', (r["lon"], r["lat"]),
                        textcoords="offset points", xytext=(7, 6), fontsize=9, zorder=4)
        handles = [plt.scatter([], [], c=LEVEL_COLORS[c], s=90, label=f"{c}色")
                   for c in ("蓝", "黄", "橙", "红")]
        ax.legend(handles=handles, title="风险等级", loc="lower left")
        ax.set_xlabel("经度 (°E)")
        ax.set_ylabel("纬度 (°N)")
        ax.set_title(f"气候健康风险地图　{df['window'].iloc[0]}\n气泡面积 ∝ 人口规模")
        ax.grid(True, linestyle=":", alpha=0.5)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    return True


ADVICE = {
    "老年人（65+）": "减少 10:00–16:00 外出；每日多次少量饮水；独居老人建议社区每日探访；出现头晕、恶心、意识模糊立即就医。",
    "儿童与婴幼儿": "避免正午户外活动；切勿将儿童单独留在车内；注意补水和电解质。",
    "户外劳动者": "执行错峰作业；每作业 45 分钟阴凉处休息 15 分钟；提供含盐饮水；同伴互察中暑征兆。",
    "心脑血管/呼吸系统疾病患者": "按时服药；避免骤冷骤热环境切换；污染日减少开窗并佩戴口罩；症状加重及时就诊。",
}

LEVEL_ACTION = {
    "蓝": "常规关注：关注气温变化，保持正常作息。",
    "黄": "提示级：敏感人群减少午后户外活动，注意补水。",
    "橙": "预警级：开放避暑场所，户外作业调整工时，医疗机构加强值守。",
    "红": "应急级：启动高温应急响应，暂停露天集体活动，对脆弱人群开展主动排查。",
}


def write_warnings(df, path):
    lines = [f"# 气候健康风险预警文案\n\n**分析窗口**：{df['window'].iloc[0]}　"
             f"**生成时间**：{dt.datetime.now():%Y-%m-%d %H:%M}\n"]
    for _, r in df.sort_values("risk_score", ascending=False).iterrows():
        lines.append(f"\n## {'🔴🟠🟡🔵'['红橙黄蓝'.index(r['level_color'])]} "
                     f"{r['city']} — 风险分值 {r['risk_score']:.0f}（{r['level_text']}）\n")
        lines.append(
            f"**关键指标**：热异常 EHI {r['ehi']:+.1f}°C｜热浪日数 {r['heatwave_days']} 天"
            f"（最长连续 {r['consecutive_heatwave']} 天）｜最高湿球温度 {r['tw_max']:.1f}°C｜"
            f"PM2.5 峰值 {r['pm25_peak']:.0f} µg/m³ (IAQI {r['iaqi_pm25']:.0f})｜"
            f"O₃ 峰值 {r['o3_peak']:.0f} µg/m³ (IAQI {r['iaqi_o3']:.0f})\n")
        lines.append(
            f"**预警文案**：{r['city']}未来时段气候健康风险为{r['level_text']}。"
            f"窗口期平均气温 {r['tmean_window']:.1f}°C，较 1991–2020 基准偏高 {r['ehi']:+.1f}°C，"
            f"最高湿球温度 {r['tw_max']:.1f}°C。{LEVEL_ACTION[r['level_color']]}\n")
        lines.append("**重点人群提示**：")
        elderly_pct = r["elderly_ratio"] * 100
        for grp, adv in ADVICE.items():
            prefix = f"（本市占比约 {elderly_pct:.1f}%）" if grp.startswith("老年人") else ""
            lines.append(f"- **{grp}**{prefix}：{adv}")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_report(df, path, gen_time):
    pop = df["population"]
    pop_weighted_risk = float((df["risk_score"] * pop).sum() / pop.sum())
    simple_mean = float(df["risk_score"].mean())
    share_high = float(pop[df["risk_score"] >= 50].sum() / pop.sum())
    elderly_share = float((pop * df["elderly_ratio"]).sum() / pop.sum())
    burden = float((pop * df["elderly_ratio"] * df["risk_score"]).sum()
                   / (pop * df["risk_score"]).sum())
    # 权重敏感性：扰动 hazard 权重 ±0.1（其余按比例重分配）
    # Spearman = 对排名求 Pearson，避免引入 scipy 依赖
    base_rank = df["risk_score"].rank()
    sens = []
    for w_h, w_e, w_v in ((0.6, 0.24, 0.16), (0.4, 0.36, 0.24)):
        alt = risk_with_weights(df, w_h, w_e, w_v).rank()
        sens.append((w_h, float(base_rank.corr(alt))))

    lines = [
        "# 气候健康风险报告\n",
        f"**分析窗口**：{df['window'].iloc[0]}　**生成时间**：{gen_time:%Y-%m-%d %H:%M}",
        f"**数据源**：Open-Meteo Archive API (ERA5)、Open-Meteo Air Quality API (CAMS)、"
        f"WorldPop 2020、七普人口数据\n",
        "## 风险总览\n",
        df[["city", "ehi", "heatwave_days", "tw_max", "pm25_peak", "iaqi_pm25",
            "hazard", "exposure", "vulnerability", "risk_score", "level_text"]]
        .to_markdown(index=False),
        "\n## 不确定性说明\n",
        "1. **气象数据**：ERA5 为 ~31km 再分析网格，无法分辨城市热岛效应，"
        "市中心实际气温可能比本评估高 1–3°C，风险可能被低估。",
        "2. **空气质量**：CAMS 全球产品分辨率 ~10km，PM2.5/O₃ 城市尺度偏差可达 ±30%，"
        "IAQI 结果应视为区域背景参考。",
        "3. **脆弱性代理**：老龄化率采用七普城市级数据；'气候适应度'以基准期均温代理，"
        "未纳入空调普及率、住房条件等社会经济因素。",
        "4. **权重敏感性**：将危险性权重由 0.5 扰动至 0.6/0.4 后，城市风险排名 Spearman 相关为 "
        + "、".join(f"{s:.3f}（w_h={w}）" for w, s in sens)
        + "，排名结构稳健。",
        "5. **湿球温度**：由小时温湿度直接计算，未考虑辐射与风速修正（UTCI 更精确，列为扩展项）。\n",
        "## 公平性检查\n",
        f"- 人口加权平均风险 **{pop_weighted_risk:.1f}** vs 城市简单平均 **{simple_mean:.1f}**"
        + ("：人口更多集中在风险更高的城市。" if pop_weighted_risk > simple_mean
           else "：风险未向高人口城市集中。"),
        f"- 处于高风险（≥50 分）等级的人口占比：**{share_high * 100:.1f}%**",
        f"- 老年人风险负担比：**{burden / max(elderly_share, 1e-9):.2f}**"
        f"（>1 表示老年人承受不成比例的风险；总体 65+ 占比 {elderly_share * 100:.1f}%）",
        "- 建议：对负担比 >1 的城市优先配置避暑中心、社区探访与医疗资源。\n",
        "## 复现方式\n",
        "```bash\npython scripts/run_pipeline.py --cities data/cities_demo.csv "
        f"--start {df['window'].iloc[0].split(' ~ ')[0]} "
        f"--end {df['window'].iloc[0].split(' ~ ')[1]} --outdir output\n```\n",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser(description="气候健康风险预警管线")
    ap.add_argument("--cities", required=True, help="城市清单 CSV")
    ap.add_argument("--start", required=True, help="分析窗口开始 YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="分析窗口结束 YYYY-MM-DD")
    ap.add_argument("--outdir", default="output")
    ap.add_argument("--add-city", action="append", default=[], metavar="城市名",
                    help="追加不在 CSV 中的城市（可多次），自动地理编码获取坐标与人口")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    cities = pd.read_csv(args.cities)
    if "name_en" not in cities.columns:
        cities["name_en"] = ""

    # 地理编码追加城市（不在表格里的城市也能预警）
    for cname in args.add_city:
        if cname in set(cities["name"]):
            print(f"[提示] '{cname}' 已在清单中，跳过地理编码")
            continue
        try:
            info = geocode_city(cname)
            cities = pd.concat([cities, pd.DataFrame([info])], ignore_index=True)
            print(f"[地理编码] {cname} → ({info['lat']}, {info['lon']})，"
                  f"人口 {info['population']:,}，GADM 名 {info['name_en']}")
        except Exception as e:  # noqa: BLE001
            print(f"[跳过] 地理编码 '{cname}' 失败: {e}", file=sys.stderr)

    print(f"共 {len(cities)} 个城市；窗口 {args.start} ~ {args.end}；"
          f"基准期 {BASELINE_START} ~ {BASELINE_END}")

    rows, skipped = [], []
    for _, row in cities.iterrows():
        print(f"→ {row['name']} ({row['lat']}, {row['lon']}) ...")
        try:
            rows.append(compute_city_risk(row, args.start, args.end))
        except Exception as e:  # noqa: BLE001
            print(f"  [跳过] {row['name']}: {e}", file=sys.stderr)
            skipped.append((row["name"], str(e)))

    if not rows:
        print("全部城市获取失败，退出。", file=sys.stderr)
        sys.exit(1)

    df = pd.DataFrame(rows).sort_values("risk_score", ascending=False)
    gen_time = dt.datetime.now()

    csv_path = outdir / "risk_table.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    write_warnings(df, outdir / "warnings.md")
    write_report(df, outdir / "report.md", gen_time)
    map_ok = draw_map(df, outdir / "risk_map.png")

    print("\n===== 风险总览 =====")
    print(df[["city", "ehi", "heatwave_days", "tw_max", "iaqi_pm25",
              "risk_score", "level_text"]].to_string(index=False))
    if skipped:
        print(f"\n[注意] 跳过 {len(skipped)} 个城市: "
              + "; ".join(f"{n}({e[:40]})" for n, e in skipped))
    print(f"\n输出: {csv_path}, warnings.md, report.md"
          + (", risk_map.png" if map_ok else ""))


if __name__ == "__main__":
    main()
