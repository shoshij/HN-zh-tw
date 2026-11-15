"""把混進譯文的簡體字改回繁體字。

模型偶爾會在一整句繁體字裡夾一個簡體字（例如「星舰」「大胆」）。這裡用 OpenCC 的字典
找出「只會在簡體字裡出現」的字，只把那些字換成台灣寫法，其他字一個都不動。
不能整段丟給 OpenCC 轉：繁體文字裡的「干擾」「台灣」「皇后」它會轉錯（幹擾、臺灣、皇後）。
"""
from __future__ import annotations

import os
import re

import opencc

# 這些字台灣的標準寫法就是這樣（雇主、發霉、日晒、佳肴、苧麻、疱疹），但 OpenCC 字典會把它們
# 轉成別的字（僱、黴、曬、餚、薴、皰），所以明確排除。
TAIWAN_OK = set("雇霉晒肴苧疱")

_s2twp = opencc.OpenCC("s2twp")  # 簡體 → 台灣繁體，連用語一起換（软件 → 軟體）
_t2s = opencc.OpenCC("t2s")
_t2tw = opencc.OpenCC("t2tw")    # 正體異體字 → 台灣標準寫法（羣 → 群）


def _load() -> dict[str, list[str]]:
    """從 OpenCC 的字典挑出「只會在簡體字裡出現」的字：{簡體字: [可能的繁體寫法, ...]}。

    排除本身也是合法繁體字的（后、干、台、里…），以及只是異體字差異的（群/羣、床/牀）。
    """
    path = os.path.join(os.path.dirname(opencc.__file__), "dictionary", "STCharacters.txt")
    table = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            c, cands = parts[0], parts[1].split()
            if c in cands or c in TAIWAN_OK or c in {_t2tw.convert(t) for t in cands}:
                continue
            table[c] = cands
    return table


SIMPLIFIED = _load()
_PATTERN = re.compile("[" + "".join(re.escape(c) for c in SIMPLIFIED) + "]")


def find(text: str) -> str:
    """回傳文字裡出現的簡體字（去重、照出現順序）；沒有就是空字串。"""
    seen: list[str] = []
    for c in _PATTERN.findall(text):
        if c not in seen:
            seen.append(c)
    return "".join(seen)


def to_traditional(text: str) -> str:
    """只把簡體字換成繁體字，其餘一字不動。一字有多種繁體寫法的（发 → 發/髮）看前後文決定。"""
    if not _PATTERN.search(text):
        return text
    out = list(text)
    for m in _PATTERN.finditer(text):
        i, c = m.start(), m.group()
        cands = SIMPLIFIED[c]
        out[i] = cands[0]
        if len(cands) > 1:
            lo = max(0, i - 3)
            window = text[lo:i + 4]
            # 先把前後幾個字全轉成簡體再轉回台灣繁體，OpenCC 的詞典才認得出「頭发」這種混搭；
            # 只取被標記的那個字的結果，前後的字不用它的。
            conv = _s2twp.convert(_t2s.convert(window))
            if len(conv) == len(window) and conv[i - lo] in cands:
                out[i] = conv[i - lo]
    return "".join(out)


def fix(text: str) -> tuple[str, str]:
    """回傳 (修正後的文字, 修正說明)。沒有簡體字時文字原樣回傳、說明是空字串。"""
    fixed = to_traditional(text)
    if fixed == text:
        return text, ""
    pairs: list[str] = []
    for a, b in zip(text, fixed, strict=True):
        if a != b and f"{a}→{b}" not in pairs:
            pairs.append(f"{a}→{b}")
    return fixed, "、".join(pairs)
