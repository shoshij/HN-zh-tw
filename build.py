#!/usr/bin/env python3
"""第二步：讀 data.json，產生 site/index.html 與 site/feed.xml。

用法：
  python build.py
  python build.py --data /tmp/test.json   # 本機測試：讀別的資料檔
"""
import argparse
from pathlib import Path

from hnzh import render, store

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, help="改用別的資料檔（本機測試用）")
    args = ap.parse_args()
    if args.data:
        store.DATA_FILE = args.data.resolve()
    render.build()
