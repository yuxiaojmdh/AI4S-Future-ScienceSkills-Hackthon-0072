#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""方法论单元测试：Stull 湿球公式、IAQI 断点插值、风险分级。

运行: python scripts/test_methods.py （无需 pytest）
"""
import numpy as np

from run_pipeline import (IAQI_O3_1H, IAQI_PM25, iaqi, risk_level,
                          stull_wetbulb)


def test_stull_wetbulb():
    # 已知参考：T=30°C, RH=50% → Tw ≈ 22.3°C（Stull 2011 表值附近）
    tw = float(stull_wetbulb(np.array([30.0]), np.array([50.0]))[0])
    assert 21.5 < tw < 23.0, f"Tw(30,50%)={tw}"
    # 饱和时湿球≈干球
    tw_sat = float(stull_wetbulb(np.array([25.0]), np.array([99.0]))[0])
    assert abs(tw_sat - 25.0) < 1.0, f"Tw(25,99%)={tw_sat}"
    # 单调性：湿度升高 → 湿球升高
    tw_dry = float(stull_wetbulb(np.array([35.0]), np.array([20.0]))[0])
    tw_humid = float(stull_wetbulb(np.array([35.0]), np.array([80.0]))[0])
    assert tw_humid > tw_dry


def test_iaqi_pm25():
    assert iaqi(35, IAQI_PM25) == 50
    assert iaqi(75, IAQI_PM25) == 100
    assert iaqi(115, IAQI_PM25) == 150
    assert abs(iaqi(55, IAQI_PM25) - 75.0) < 1e-6   # 断点内线性内插
    assert iaqi(600, IAQI_PM25) == 500               # 超上限截断
    assert iaqi(0, IAQI_PM25) == 0


def test_iaqi_o3():
    assert iaqi(160, IAQI_O3_1H) == 50
    assert iaqi(200, IAQI_O3_1H) == 100
    assert abs(iaqi(180, IAQI_O3_1H) - 75.0) < 1e-6


def test_risk_level():
    assert risk_level(75)[0] == "红"
    assert risk_level(60)[0] == "橙"
    assert risk_level(40)[0] == "黄"
    assert risk_level(10)[0] == "蓝"
    assert risk_level(70)[0] == "红"    # 边界含上界
    assert risk_level(50)[0] == "橙"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("\n全部单元测试通过 ✓")
