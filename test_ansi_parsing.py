#!/usr/bin/env python3
"""测试 ANSI 剥离和模式匹配"""

import re

# 从日志中提取的原始输出
raw_output = """[?2026h
[38;2;255;193;7m────────────────────────────────────────────────────────────────────────────────[39m
[1C[38;2;255;193;7m[1mAccessing[1Cworkspace:[22m[39m

[1C[1m/tmp/test-claude-debug[22m

[1CQuick[1Csafety[1Ccheck:[1CIs[1Cthis[1Ca[1Cproject[1Cyou[1Ccreated[1Cor[1Cone[1Cyou[1Ctrust?[1C(Like[1Cyour
[2Cown[1Ccode,[1Ca[1Cwell-known[1Copen[1Csource[1Cproject,[1Cor[1Cwork[1Cfrom[1Cyour[1Cteam).[1CIf[1Cnot,
[1Ctake[1Ca[1Cmoment[1Cto[1Creview[1Cwhat's[1Cin[1Cthis[1Cfolder[1Cfirst.

[1CClaude[1CCode'll[1Cbe[1Cable[1Cto[1Cread,[1Cedit,[1Cand[1Cexecute[1Cfiles[1Chere.

[1C]8;;https://code.claude.com/docs/en/security[38;2;153;153;153mSecurity guide[39m]8;;

[1C[38;2;177;185;249m❯[1C[38;2;153;153;153m1.[1C[38;2;177;185;249mYes,[1CI[1Ctrust[1Cthis[1Cfolder[39m
[3C[38;2;153;153;153m2.[1C[39mNo,[1Cexit

[1C[38;2;153;153;153mEnter[1Cto[1Cconfirm[1C·[1CEsc[1Cto[1Ccancel[39m
[?2026l[?2004h[?1004h[?25l"""

def strip_ansi(text):
    """剥离 ANSI 转义码 - 改进版"""
    import re
    
    # \x1b[ 开头的控制序列
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    
    # OSC 序列 (如超链接)
    text = re.sub(r'\x1b]8;;[^\x1b]*\x1b\\', '', text)
    
    # 其他控制序列
    text = re.sub(r'\x1b[^\[]', '', text)
    
    # 问号开头的模式
    text = re.sub(r'\[\?[0-9;]*[a-zA-Z]', '', text)
    
    # CSI 移动光标 - 用空格替换（保留布局）
    # [1C = 右移1格, [2C = 右移2格
    def replace_cursor_move(match):
        m = re.match(r'\[(\d+)C', match.group())
        if m:
            count = int(m.group(1))
            return ' ' * count
        return ' '
    
    text = re.sub(r'\[\d+C', replace_cursor_move, text)
    
    # 其他 CSI 序列
    text = re.sub(r'\[[0-9;]*m', '', text)  # 颜色/样式
    
    return text

print("原始输出（前 200 字符）:")
print(repr(raw_output[:200]))
print("\n" + "="*60)

clean_text = strip_ansi(raw_output)
print("\n清理后的文本:")
print(clean_text)
print("\n" + "="*60)

# 测试各种匹配
patterns = [
    ("Is this a project you created or one you trust?", r"Is this a project you created or one you trust\?"),
    ("Yes, I trust this folder", r"Yes, I trust this folder"),
    ("Enter to confirm", r"Enter to confirm"),
    ("❯", r"❯"),
    ("1.", r"1\."),
]

print("\n模式匹配测试:")
for desc, pattern in patterns:
    match = re.search(pattern, clean_text, re.IGNORECASE)
    status = "✅" if match else "❌"
    print(f"{status} '{desc}' -> {pattern}")
    if match:
        print(f"   匹配位置: {match.start()}-{match.end()}")
        print(f"   匹配内容: {repr(match.group())}")
