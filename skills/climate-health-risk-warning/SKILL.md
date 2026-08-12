---
name: climate-health-risk-warning
description: "Use when generating city/community-level heat-health risk early warnings by fusing ERA5 reanalysis meteorology, CAMS air quality, and population vulnerability data. Produces risk maps, tiered warning texts, uncertainty statements, and equity checks as verifiable, reproducible outputs."
version: 1.0.0
author: AI4S Hackathon Team
license: MIT
metadata:
  hermes:
    tags: [climate, health-risk, heatwave, air-quality, ai-for-science, early-warning]
    related_skills: [ai-climate-data-analyzer]
---

# 气候健康风险预警 Skill（Climate Health Risk Warning）

## Overview

本 Skill 解决赛题 3 的核心问题：**融合 ERA5 温湿度/热浪指标、CAMS 空气污染数据与人口脆弱性数据，生成城市/社区级健康风险提示**。

它回答三个科学问题：
1. **危险性（Hazard）**：当前时段的热应力（干热 + 湿热）与空气污染有多严重？相对气候基准态偏离多少？
2. **暴露度（Exposure）**：有多少人口暴露在风险中？
3. **脆弱性（Vulnerability）**：人群的年龄结构与气候适应能力如何？

最终输出五项赛题要求的交付物：**风险地图、重点人群提示、预警文案、不确定性说明、公平性检查**。全部结果可由 `scripts/run_pipeline.py` 一键复现，固定随机源、确定性阈值、无随机成分。

## When to Use

- 需要对一组城市/区域生成热浪-健康风险分级预警时
- 需要量化"高温 + 高湿 + 空气污染"复合事件的健康风险时
- 需要输出面向公众的预警文案与面向决策者的公平性分析时
- 作为更大决策系统（应急管理、公共卫生调度）中可被调用的风险计算模块

**Don't use for:**
- 临床级个体健康预测（本 Skill 是人群级风险评估，非医疗诊断）
- 未来气候预估（本 Skill 基于再分析/近实时数据，不做气候模式降尺度）
- 无网络环境（数据源均为在线 API/下载，可先缓存后离线计算）

## 数据源（全部免费、免注册、已实测验证）

| 数据 | 来源 | 获取方式 | 验证状态 |
|---|---|---|---|
| 气温/湿度/风（日值+小时值，ERA5 再分析） | Open-Meteo Archive API | `https://archive-api.open-meteo.com/v1/archive`，GET 参数式调用，无需 API key | ✅ 实测通过 |
| PM2.5 / PM10 / O₃ / NO₂（小时值，CAMS 全球） | Open-Meteo Air Quality API | `https://air-quality-api.open-meteo.com/v1/air-quality`，无需 API key | ✅ 实测通过 |
| 人口密度栅格（~1km，2020） | WorldPop | `https://data.worldpop.org/GIS/Population/Global_2000_2020/2020/CHN/chn_ppp_2020_UNadj.tif` | ✅ URL 可达 |
| 行政区划边界（省/市/县全层级） | GADM 4.1 | `https://geodata.ucdavis.edu/gadm/gadm4.1/gpkg/gadm41_CHN.gpkg`（约 76MB，单文件含全部层级；**已随仓库 data/ 分发**，地图自动叠加省级边界） | ✅ 已下载并验证（37 个省级要素） |
| 城市人口与老龄化率 | 第七次全国人口普查公报（公开） | 随仓库提供 `data/cities_demo.csv` | ✅ 随仓库分发 |

> **已验证的坑**：Open-Meteo 空气质量 API 的 `daily=pm2_5_max` 等日聚合变量名**不被接受**（返回 Data corrupted 错误），必须请求 `hourly=pm2_5,pm10,ozone,nitrogen_dioxide` 后在本地聚合为日值。

## 方法论

### 指标定义

**1.  excess Heat Index（EHI，热异常强度）**
```
EHI = mean(Tmean_分析窗口) − P90(Tmean_基准期同日历窗口)
```
基准期取 WMO 标准气候平均期 1991–2020，日历窗口向两侧各扩展 ±15 天以稳定分位数估计。EHI 衡量"相对本地气候的异常程度"，避免把"本来就热的地方"一律判为高风险。

**2. 热浪日数**：`Tmax ≥ 35°C` 的日数（中国气象业务标准），并检测 ≥3 天连续热浪。

**3. 湿热应力（湿球温度）**：Stull (2011) 近似公式（适用 RH 5–99%，T −20~50°C）：
```
Tw = T·atan(0.151977·√(RH+8.313659)) + atan(T+RH) − atan(RH−1.676331)
     + 0.00391838·RH^1.5·atan(0.023101·RH) − 4.686035
```
用分析窗口小时值计算 Tw 最大值。Tw ≥ 26°C 进入健康危险区，≥ 31°C 接近人体耐受极限。

**4. 空气污染（IAQI 分项指数）**：按中国 HJ 633-2012 断点线性内插，将 PM2.5 日最大浓度、O₃ 小时最大浓度换算为 IAQI（0–500）。

### 风险模型

各分量归一化到 0–100 后加权：

```
Hazard      = 100 · (0.4·clip(EHI/8) + 0.3·clip((Tw_max−26)/8) + 0.3·clip(max(IAQI_PM25, IAQI_O3)/300))
Exposure    = 100 · clip((log10(人口)−5)/3)          # 10万→0，1亿→100
Vulnerability = 100 · (0.6·clip(65+占比/0.30) + 0.4·(1−clip((基准期均温−5)/20)))
Risk = 0.5·Hazard + 0.3·Exposure + 0.2·Vulnerability
```
脆弱性第二项是"气候适应度"代理：常年寒冷地区人群对热浪适应能力更低。权重取值依据 IPCC AR6 WGII 风险框架（Hazard×Exposure×Vulnerability）的常用实践，**权重敏感性分析自动运行并写入不确定性报告**。

### 四级风险分级

| 分级 | 颜色 | Risk 分值 | 含义 |
|---|---|---|---|
| IV 级 | 蓝 | < 30 | 低风险，常规关注 |
| III 级 | 黄 | 30–50 | 中风险，提示敏感人群 |
| II 级 | 橙 | 50–70 | 高风险，启动防护 |
| I 级 | 红 | ≥ 70 | 极高风险，应急响应 |

## Workflow

1. **准备城市清单**：CSV 含 `name,lat,lon,population,elderly_ratio`。完成标准：每行坐标可被 API 接受（纬度 ±90、经度 ±180），人口为正数。
2. **获取气象数据**：对每城市调用 Archive API——分析窗口取日值 + 小时值（温度、湿度），基准期 1991–2020 取日值。内置 3 次指数退避重试；单城市重试耗尽则记录并跳过（不中断整体）。完成标准：每城市窗口日数 = 预期天数，NaN 比例 < 5%。
3. **获取空气质量**：Air Quality API 小时值 → 本地聚合为日最大。完成标准：PM2.5 与 O₃ 序列完整。
4. **计算指标与风险**：EHI、热浪日数、Tw_max、IAQI → Hazard/Exposure/Vulnerability → Risk 与分级。完成标准：所有分量落在 [0,100]。
5. **生成交付物**（见下节）。完成标准：5 类输出文件全部落盘且非空。
6. **自检**：运行 `python scripts/test_methods.py`（公式单元测试）+ 核对 Verification Checklist。完成标准：全部断言通过。

```powershell
# 一键运行（示例：2026年7月1–7日，10个演示城市）
pip install -r requirements.txt
python scripts/run_pipeline.py --cities data/cities_demo.csv --start 2026-07-01 --end 2026-07-07 --outdir output
python scripts/test_methods.py

# 对不在表格里的城市预警：--add-city 按城市名自动地理编码（坐标+人口），可多次使用
python scripts/run_pipeline.py --cities data/cities_demo.csv --start 2026-07-01 --end 2026-07-07 --outdir output --add-city 苏州 --add-city 杭州
```

**任意城市预警说明**：`--add-city` 调用 Open-Meteo 免费地理编码 API（中文名自动用 `language=zh` 查询），自动获取坐标与人口；老龄化率无逐城市实时公开来源，默认取 0.15 并在 notes 标注（可在 CSV 中补充真实值后改用 `--cities`）。地理编码 API 免费档有每分钟限流，管线遇 429 自动等待 62s 重试。

## 输出规范（对应赛题五项交付物）

| 赛题要求 | 输出文件 | 内容 |
|---|---|---|
| 风险地图 | `output/risk_map.png` | **行政区填色图（choropleth）**：颜色只覆盖该城市的行政区域（GADM 市级图层，按英文名匹配，匹配失败自动用坐标点-多边形空间匹配兜底）；右侧附暴露人口规模条形图。无 GADM 文件时自动降级为气泡图 |
| 重点人群提示 | `output/warnings.md` 内嵌 | 按等级模板生成：老年人、儿童、户外劳动者、心脑血管/呼吸系统疾病患者分类建议 |
| 预警文案 | `output/warnings.md` | 每城市一段可直接发布的中文预警文本，含关键指标数值 |
| 不确定性说明 | `output/report.md` §不确定性 | 数据源误差（ERA5 网格无法分辨城市热岛、CAMS ~10km 分辨率）、脆弱性代理假设、权重敏感性（自动计算扰动权重下的 Spearman 秩相关） |
| 公平性检查 | `output/report.md` §公平性 | 人口加权风险 vs 简单平均、高风险等级人口占比、老年人风险负担比（>1 表示老年人承受不成比例的风险） |

另输出 `output/risk_table.csv`：全部分量分值，供二次开发调用。

## Common Pitfalls

1. **用 daily 聚合变量名调用空气质量 API** → 报 `Data corrupted`。必须用 hourly 变量本地聚合。
2. **时间序列数据随机切分做验证** → 泄漏。本 Skill 基准期与窗口严格分离，EHI 只用历史数据。
3. **把绝对温度当风险** → 热带城市永远"最高风险"。必须用 EHI（相对基准异常）。
4. **忽略湿度** → 干热 38°C 与湿热 38°C 健康后果差异巨大，必须计算 Tw。
5. **WorldPop 1km UNadjusted 的旧路径已失效（404）** → 使用上文表格中的已验证路径。
6. **不固定计算参数就声称可复现** → 本管线无随机成分，所有阈值/权重写入代码与本文档，同一输入必得同一输出。
7. **GADM 同名城市误匹配**：江苏苏州与安徽宿州英文名同为 "Suzhou"，纯名称匹配会填错行政区。本管线以**坐标点-多边形空间匹配为主**、名称匹配为兜底，已验证可正确区分。
8. **Open-Meteo 免费档限流**：每分钟与每小时均有请求上限。管线内置 1.5s 节流、429 自动等待 62s 重试、**磁盘缓存**（`cache/`，成功响应落盘，重跑零配额消耗且可离线复现）。

## Verification Checklist

- [ ] `test_methods.py` 全部断言通过（Stull 公式、IAQI 断点、分级阈值）
- [ ] 每城市 EHI、Tw_max、IAQI 数值在物理合理范围（EHI −10~+15°C，Tw < 35°C，IAQI ≤ 500）
- [ ] `risk_table.csv` 所有分量 ∈ [0,100]，Risk 与分级映射一致
- [ ] `risk_map.png` 生成且城市位置正确
- [ ] `warnings.md` 每城市含预警文案 + 四类重点人群提示
- [ ] `report.md` 含不确定性说明（含权重敏感性数值）与公平性检查三项统计量
- [ ] 重跑一次，`risk_table.csv` 逐字节一致（可复现性）

## Reproducibility Statement

数据源版本：Open-Meteo Archive/Air Quality API（ERA5 / CAMS，调用时间戳自动写入 report.md）；WorldPop 2020 UNadjusted；GADM 4.1。计算确定性：无随机数、无 GPU 非确定性算子。代码、数据清单、参数全部随仓库开源（MIT）。
