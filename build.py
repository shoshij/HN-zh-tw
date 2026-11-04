#!/usr/bin/env python3
"""第二步：讀 data.json，產生 site/index.html 與 site/feed.xml。

用法：python build.py
"""
from hnzh import render

if __name__ == "__main__":
    render.build()
