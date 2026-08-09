#!/usr/bin/env python3
"""
caveman-compress — 压缩 swarm skill 文件 (正则版)

应用 caveman 规则压缩 markdown 文件中的英文/中文 prose。
保留: 代码块、URL、文件路径、YAML frontmatter、标题结构。
压缩: 填充词(filler)、冠词(articles)、客套话(pleasantries)。

用法:
    python3 caveman_compress.py <file.md> [--dry-run] [--backup]

输出: 压缩后的内容写入原文件，备份保存为 <file>.original.md
"""

import argparse
import re
import sys
from pathlib import Path

# ── 压缩规则 ──

# 英文填充词 (大小写不敏感，边界匹配)
ENGLISH_FILLER = [
    r'\bactually\b', r'\bbasically\b', r'\bcertainly\b', r'\bdefinitely\b',
    r'\bessentially\b', r'\bfrankly\b', r'\bhonestly\b', r'\bindeed\b',
    r'\bjust\b', r'\bliterally\b', r'\bmerely\b', r'\bobviously\b',
    r'\bquite\b', r'\brather\b', r'\breally\b', r'\bsimply\b',
    r'\bsomewhat\b', r'\btruly\b', r'\btypically\b', r'\busually\b',
    r'\bvery\b', r'\bvirtually\b',
]

# 英文客套话
ENGLISH_PLEASANTRIES = [
    r'\bplease note that\b',
    r'\bit is worth noting that\b',
    r'\bit should be noted that\b',
    r'\bnote that\b',
    r'\bi would like to\b',
    r'\bwe can see that\b',
    r'\bas you can see\b',
    r'\bas shown above\b',
    r'\bas mentioned earlier\b',
    r'\bin order to\b',
    r'\bfor the purpose of\b',
    r'\bin the case of\b',
    r'\bin terms of\b',
    r'\bwith respect to\b',
    r'\bwith regard to\b',
    r'\bin this case\b',
    r'\bin this example\b',
    r'\bthis is because\b',
    r'\bthis means that\b',
    r'\bthe fact that\b',
    r'\bdue to the fact that\b',
    r'\bin the event that\b',
    r'\bat the present time\b',
    r'\bin a timely manner\b',
    r'\bin the process of\b',
]

# 英文冠词 (只删除不用来区分语义的)
# 保守策略: 只删孤立的 "the" 在不影响可读性的位置
ENGLISH_ARTICLES = [
    r'\bthe\b(?=\s+(?:following|above|below|previous|next|same|first|second|third|last))',
    r'\ba\b(?=\s+(?:new|different|single|specific|particular))',
    r'\ban\b(?=\s+(?:existing|alternative|option|example))',
]

# 中文填充词
CHINESE_FILLER = [
    r'其实', r'基本上', r'实际上', r'显然', r'很明显',
    r'当然', r'确实', r'真的', r'的话', r'来说',
    r'一般而言', r'通常情况下', r'一般来说',
]

# 中文冗余表达 → 简写
CHINESE_REDUNDANT = [
    (r'进行处理', '处理'),
    (r'进行操作', '操作'),
    (r'进行配置', '配置'),
    (r'进行分析', '分析'),
    (r'进行测试', '测试'),
    (r'进行扫描', '扫描'),
    (r'加以利用', '利用'),
    (r'予以考虑', '考虑'),
    (r'可以用来', '可用来'),
    (r'能够用来', '可用来'),
    (r'需要注意到', '注意'),
    (r'需要注意的是', '注意'),
    (r'值得注意的是', '注意'),
    (r'在此基础之上', '基于此'),
    (r'在大多数情况下', '多数情况'),
    (r'这是由于', '因'),
    (r'这是因为', '因'),
    (r'根据上述分析', '综上'),
    (r'综上所述', '综上'),
]


def compress_text(text: str) -> str:
    """压缩一段 prose (非代码块内容)。"""

    # 1. 英文填充词
    for pattern in ENGLISH_FILLER:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)

    # 2. 英文客套话
    for pattern in ENGLISH_PLEASANTRIES:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)

    # 3. 英文保守冠词
    for pattern in ENGLISH_ARTICLES:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)

    # 4. 中文填充词
    for pattern in CHINESE_FILLER:
        text = text.replace(pattern, '')

    # 5. 中文冗余表达
    for old, new in CHINESE_REDUNDANT:
        text = text.replace(old, new)

    # 6. 清理多余空白
    text = re.sub(r'  +', ' ', text)        # 多空格 → 单空格
    text = re.sub(r'\n{3,}', '\n\n', text)  # 多余空行
    text = re.sub(r'^\s+$', '', text, flags=re.MULTILINE)  # 纯空白行 → 空

    return text


def compress_file(filepath: str, dry_run: bool = False, backup: bool = True) -> dict:
    """压缩一个 markdown 文件。"""
    path = Path(filepath)
    if not path.exists():
        return {"error": f"file not found: {filepath}"}

    original = path.read_text(encoding="utf-8")
    original_lines = original.count('\n') + 1

    # 分离 YAML frontmatter、代码块、普通文本
    lines = original.split('\n')
    result_lines = []
    in_code_block = False
    in_frontmatter = False
    frontmatter_done = False

    for i, line in enumerate(lines):
        # YAML frontmatter (--- start)
        if i == 0 and line.strip() == '---':
            in_frontmatter = True
            result_lines.append(line)
            continue
        if in_frontmatter:
            result_lines.append(line)
            if line.strip() == '---' and i > 0:
                in_frontmatter = False
                frontmatter_done = True
            continue

        # Code blocks (```)
        if line.strip().startswith('```'):
            in_code_block = not in_code_block
            result_lines.append(line)
            continue

        if in_code_block:
            result_lines.append(line)
            continue

        # URLs and image links — preserve
        if re.match(r'^\s*\[.*\]\(https?://', line) or re.match(r'^\s*https?://', line):
            result_lines.append(line)
            continue

        # Compress the line
        compressed = compress_text(line)
        result_lines.append(compressed)

    compressed_text = '\n'.join(result_lines)
    compressed_lines = compressed_text.count('\n') + 1

    stats = {
        "file": str(path),
        "original_lines": original_lines,
        "compressed_lines": compressed_lines,
        "original_bytes": len(original),
        "compressed_bytes": len(compressed_text),
        "reduction": f"{(1 - len(compressed_text)/max(1,len(original)))*100:.1f}%",
    }

    if dry_run:
        print(f"  {path.name}: {stats['reduction']} ({stats['original_lines']}→{stats['compressed_lines']} lines)")
        return stats

    if backup:
        backup_path = path.with_suffix(path.suffix + '.original.md')
        backup_path.write_text(original, encoding="utf-8")
        stats["backup"] = str(backup_path)

    path.write_text(compressed_text, encoding="utf-8")
    return stats


def main():
    ap = argparse.ArgumentParser(description="caveman-compress — 压缩 markdown prose")
    ap.add_argument("files", nargs="+", help="要压缩的 markdown 文件")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写入")
    ap.add_argument("--no-backup", action="store_true", help="不创建备份文件")
    args = ap.parse_args()

    total_orig = 0
    total_compressed = 0

    for f in args.files:
        stats = compress_file(f, dry_run=args.dry_run, backup=not args.no_backup)
        if "error" in stats:
            print(f"  ❌ {f}: {stats['error']}")
            continue
        print(f"  {Path(f).name}: {stats['reduction']} "
              f"({stats['original_lines']}→{stats['compressed_lines']} lines, "
              f"{stats['original_bytes']}→{stats['compressed_bytes']} bytes)")
        total_orig += stats["original_bytes"]
        total_compressed += stats["compressed_bytes"]

    if not args.dry_run:
        total_reduction = (1 - total_compressed / max(1, total_orig)) * 100
        print(f"  Total: {total_orig}→{total_compressed} bytes ({total_reduction:.1f}% reduction)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
