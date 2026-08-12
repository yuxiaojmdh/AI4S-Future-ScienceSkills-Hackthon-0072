# 气候健康风险预警 Skill（AI4S Hackathon 赛题 3）

融合 **ERA5 气象再分析 + CAMS 空气质量 + 人口脆弱性**，生成城市级热浪-健康风险分级预警。

> 完整方法论、数据源验证记录、输出规范见 **[SKILL.md](SKILL.md)**（本仓库核心交付物）。

## 快速开始

```powershell
pip install -r requirements.txt
python scripts/run_pipeline.py --cities data/cities_demo.csv --start 2026-07-01 --end 2026-07-07 --outdir output
python scripts/test_methods.py   # 方法论单元测试
```

**对不在表格里的城市预警**（自动地理编码获取坐标+人口）：

```powershell
python scripts/run_pipeline.py --cities data/cities_demo.csv --start 2026-07-01 --end 2026-07-07 --outdir output --add-city 苏州 --add-city 杭州
```

**针对特定日期**：`--start` / `--end` 指定任意历史日期窗口（ERA5 存档，约滞后 5 天；如 2026-07-01 ~ 2026-07-07）。

## 输出（`output/`）

| 文件 | 对应赛题交付物 |
|---|---|
| `risk_map.png` | 风险地图（行政区填色：颜色只覆盖该城市行政区域；右侧条形图展示暴露人口） |
| `warnings.md` | 预警文案 + 重点人群提示 |
| `report.md` | 不确定性说明 + 公平性检查 |
| `risk_table.csv` | 全部分量分值（供二次开发） |

## 目录结构

```
climate-health-risk-warning/
├── SKILL.md                 # 核心交付物：完整可复用 Skill 文档
├── README.md
├── requirements.txt
├── .gitignore
├── data/
│   ├── cities_demo.csv      # 演示城市清单（七普数据）
│   └── gadm41_CHN.gpkg      # 行政边界（76MB，不入库，用下载脚本获取）
├── scripts/
│   ├── run_pipeline.py      # 一键管线（缓存/节流/重试/跳过容错）
│   ├── test_methods.py      # 公式单元测试
│   └── download_gadm.py     # 下载 GADM 边界（可选，启用行政区填色地图）
├── cache/                   # API 响应缓存（自动生成，重跑零配额）
└── output/                  # 运行产物
```


## 许可

MIT。数据源归属：ERA5 (ECMWF/Copernicus)、CAMS (Copernicus)、WorldPop、GADM、国家统计局七普公报。
