#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载 GADM 4.1 中国行政区划边界（约 76MB，免登录）。

用法: python scripts/download_gadm.py
下载后风险地图将自动叠加市级行政区域填色。
"""
import sys
import urllib.request
from pathlib import Path

URL = "https://geodata.ucdavis.edu/gadm/gadm4.1/gpkg/gadm41_CHN.gpkg"
DEST = Path(__file__).resolve().parent.parent / "data" / "gadm41_CHN.gpkg"


def main():
    if DEST.exists() and DEST.stat().st_size > 70_000_000:
        print(f"已存在: {DEST} ({DEST.stat().st_size / 1e6:.1f}MB)，跳过下载")
        return
    DEST.parent.mkdir(parents=True, exist_ok=True)
    print(f"下载 {URL} ...")
    urllib.request.urlretrieve(URL, DEST)
    print(f"完成: {DEST} ({DEST.stat().st_size / 1e6:.1f}MB)")


if __name__ == "__main__":
    sys.exit(main())
