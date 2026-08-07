# -*- coding: utf-8 -*-
"""从 widget_ele_current.js 提取 webpack 模块数组, 打印指定模块"""
import sys

src = open("js_reverse_cache/tasks/zhilian-tdc-iv8/widget_ele_current.js", encoding="utf-8").read()
start = src.index("([") + 1
body = src[start:]

depth = 0
mods = []
cur = ""
in_str = None
i = 0
n = len(body)
while i < n:
    ch = body[i]
    if in_str:
        cur += ch
        if ch == "\\":
            cur += body[i + 1]
            i += 2
            continue
        if ch == in_str:
            in_str = None
    elif ch in ('"', "'", "`"):
        in_str = ch
        cur += ch
    elif ch in "([{":
        depth += 1
        cur += ch
    elif ch in ")]}":
        depth -= 1
        cur += ch
        if depth == 0:
            mods.append(cur)
            cur = ""
    elif ch == "," and depth == 0:
        if cur.strip():
            mods.append(cur)
            cur = ""
    else:
        cur += ch
    i += 1

print("module count:", len(mods))
idx = int(sys.argv[1]) if len(sys.argv) > 1 else 48
if idx < len(mods):
    print(f"=== module {idx} (len {len(mods[idx])}) ===")
    print(mods[idx])
else:
    print(f"module {idx} out of range")
