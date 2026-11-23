# -*- coding: utf-8 -*-
"""命令行版本：不开界面，直接把 EndNote 里的分组/文献导出成 PDF 文件夹。

示例：
    python cli.py --library "D:\\path\\My EndNote Library.enl" --list-groups
    python cli.py --library "D:\\path\\My EndNote Library.enl" --group "某个分组名" --out "D:\\out\\group1"
    python cli.py --library "D:\\path\\My EndNote Library.enl" --all --out "D:\\out\\all" --naming original
"""

from __future__ import annotations

import argparse
import os
import sys

from endnote_lib import EndNoteLibrary, LibraryError
from exporter import (
    ExportItem,
    ExportOptions,
    NamingOptions,
    export_items,
    suggest_out_dir,
)


def build_parser():
    parser = argparse.ArgumentParser(
        description="把 EndNote 文献库里选中的文献 PDF 导出到一个文件夹（供导入 ima 知识库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-l", "--library", action="append", default=[],
                        help="EndNote 库文件（.enl 或 .enlx），可重复指定多个")
    parser.add_argument("-g", "--group", action="append", default=[],
                        help="分组名称，可重复；也支持用记录号，形如 #12,#34")
    parser.add_argument("--all", action="store_true", help="导出整个库（不含回收站）")
    parser.add_argument("-o", "--out", default="", help="输出文件夹（默认自动生成）")
    parser.add_argument("--naming", default="year_author_title",
                        choices=[key for key, _label in NamingOptions.SCHEMES],
                        help="文件命名方式（默认 年份-作者-标题）")
    parser.add_argument("--template", default="{year}-{author}-{title}",
                        help="--naming custom 时的模板")
    parser.add_argument("--manifest", action="store_true", help="同时写核对清单 CSV")
    parser.add_argument("--subfolder-by-library", action="store_true",
                        help="按文献库建立子文件夹")
    parser.add_argument("--list-groups", action="store_true", help="只列出分组，不导出")
    parser.add_argument("--list-refs", action="store_true", help="只列出文献，不导出")
    parser.add_argument("--limit", type=int, default=0, help="最多导出多少篇文献（0=不限）")
    parser.add_argument("--dry-run", action="store_true", help="只显示将要导出什么")
    return parser


def parse_ids(spec):
    ids = []
    for part in spec.replace("#", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.library:
        print("请用 --library 指定至少一个 EndNote 库文件。")
        return 2

    libraries = []
    for path in args.library:
        try:
            libraries.append(EndNoteLibrary(path))
        except LibraryError as exc:
            print("无法打开 %s：%s" % (path, exc))
            return 2

    items = []
    for lib in libraries:
        selected = []
        wanted_ids = set()
        for spec in args.group:
            if spec.startswith("#"):
                wanted_ids.update(parse_ids(spec))
                continue
            matches = [g for g in lib.custom_groups if g.name == spec]
            if not matches:
                print("库 %s 中没有分组「%s」。" % (os.path.basename(lib.path), spec))
                continue
            for group in matches:
                selected.extend(lib.refs_in_group(group.id))
        if wanted_ids:
            selected.extend(lib.refs_by_ids(sorted(wanted_ids)))
        if args.all:
            selected.extend(lib.active_references)
        seen = set()
        unique = []
        for ref in selected:
            if ref.id in seen:
                continue
            seen.add(ref.id)
            unique.append(ref)
        membership = {}
        for group in lib.custom_groups:
            for ref_id in group.member_ids:
                membership.setdefault(ref_id, []).append(group.name)
        for ref in unique:
            items.append(ExportItem(
                reference=ref,
                library=lib.library_name,
                groups=tuple(membership.get(ref.id, ())),
            ))

    if args.list_groups:
        for lib in libraries:
            print("== %s ==" % os.path.basename(lib.path))
            for group in lib.custom_groups:
                print("  %-40s %4d 篇" % (group.name, len(lib.refs_in_group(group.id))))
        return 0

    if args.limit:
        items = items[: args.limit]

    if args.list_refs or args.dry_run:
        for item in items:
            ref = item.reference
            mark = "PDF" if ref.has_pdf else " - "
            print("%-5s %-10s %-6s %-22s %s"
                  % (ref.id, mark, ref.year, ref.author_label[:22], ref.display_title[:80]))
        print("合计 %d 篇，其中 %d 篇有 PDF。"
              % (len(items), sum(1 for it in items if it.reference.has_pdf)))
        if args.dry_run:
            return 0

    if not items:
        print("没有选中任何文献，什么也没做。")
        return 1

    out_dir = args.out.strip() or suggest_out_dir()
    options = ExportOptions(
        out_dir=out_dir,
        naming=NamingOptions(scheme=args.naming, template=args.template),
        write_manifest=args.manifest,
        subfolder_by_library=args.subfolder_by_library,
    )

    def progress(done, total, label):
        if done % 20 == 0 or done == total:
            sys.stdout.write("\r导出中 %d/%d" % (done, total))
            sys.stdout.flush()

    result = export_items(items, options, progress=progress)
    sys.stdout.write("\r")
    print("完成：导出 %d 个 PDF（共 %.1f MB）→ %s"
          % (result.count, result.total_bytes / 1024.0 / 1024.0, result.out_dir))
    if result.no_pdf:
        print("跳过 %d 篇没有 PDF 的文献：" % len(result.no_pdf))
        for ref in result.no_pdf[:20]:
            print("   · %s %s" % (ref.year, ref.display_title[:70]))
    if result.missing:
        print("有 %d 个附件在库中找不到文件（已跳过）。" % len(result.missing))
    if result.errors:
        print("有 %d 个文件复制失败。" % len(result.errors))
    if result.manifest_path:
        print("核对清单：%s" % result.manifest_path)
    for lib in libraries:
        lib.close()
    return 0 if result.count else 1


if __name__ == "__main__":
    sys.exit(main())
