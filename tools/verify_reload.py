# -*- coding: utf-8 -*-
"""验证：EndNote 同步/新增记录后，「重新加载」能否读到新题录和分组变化。

做法：把真实库的 sdb.eni 复制一份到临时目录（不碰你的库），
然后直接往副本里写新题录、改分组成员、加新分组，再模拟程序里的
"重新加载"（关掉重开库），核对界面数据是否更新。

运行：python tools/verify_reload.py [库文件.enl]
     （不传参数时用 $ENDNOTE_LIBRARY 或程序上次打开的库）
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from endnote_lib import EndNoteLibrary  # noqa: E402
from paths import default_library  # noqa: E402

TEST_GROUP = os.environ.get("ENDNOTE_TEST_GROUP", "")
REAL_LIBRARY = os.environ.get("ENDNOTE_LIBRARY") or default_library()


def source_sdb_path(library_path):
    """Locate the live sdb.eni that belongs to *library_path*."""
    lib = EndNoteLibrary(library_path)
    try:
        return lib.sdb_path
    finally:
        lib.close()


def make_test_library(root, sdb_source):
    """Create <root>/SyncTest.enl + SyncTest.Data/sdb/sdb.eni (copy of real DB)."""
    data_dir = os.path.join(root, "SyncTest.Data")
    os.makedirs(os.path.join(data_dir, "sdb"), exist_ok=True)
    os.makedirs(os.path.join(data_dir, "PDF"), exist_ok=True)
    enl = os.path.join(root, "SyncTest.enl")
    # the .enl only has to exist; the reader uses .Data/sdb/sdb.eni
    with open(enl, "wb") as fh:
        fh.write(b"")
    shutil.copyfile(sdb_source, os.path.join(data_dir, "sdb", "sdb.eni"))
    return enl, os.path.join(data_dir, "sdb", "sdb.eni")


def writable_connection(db_path):
    """Open the copy for writing, registering EndNote's custom SQL functions."""
    con = sqlite3.connect(db_path)
    con.create_function("EN_MAKE_SORT_KEY", -1, lambda *a: "")
    for name in ("ENCI_Base", "ENCIN_zh_CN", "ENCIN_Base", "ENCIN_zh_TW"):
        con.create_collation(name, lambda a, b: (a > b) - (a < b))
    return con


def members_blob(ids):
    return b"\x00\x00\x00\x02" + len(ids).to_bytes(4, "little") + b"".join(
        i.to_bytes(4, "little") for i in ids
    )


def parse_members(blob):
    count = int.from_bytes(blob[4:8], "little")
    return [int.from_bytes(blob[8 + i * 4:12 + i * 4], "little") for i in range(count)]


def add_reference(con, ref_id, title, author, year):
    con.execute(
        "INSERT INTO refs (id, reference_type, author, year, title, "
        "added_to_library, record_last_updated) VALUES (?, 0, ?, ?, ?, ?, ?)",
        (ref_id, author, year, title, int(time.time()), int(time.time())),
    )


def group_id_by_name(con, name):
    for gid, spec in con.execute("SELECT group_id, spec FROM groups"):
        text = spec.decode("utf-8", "replace") if isinstance(spec, bytes) else str(spec)
        if "<name>%s</name>" % name in text:
            return gid
    return None


def group_ids(con, gid):
    blob = con.execute("SELECT members FROM groups WHERE group_id=?", (gid,)).fetchone()[0]
    return parse_members(bytes(blob))


def main():
    library_arg = sys.argv[1] if len(sys.argv) > 1 else REAL_LIBRARY
    if not library_arg or not os.path.exists(library_arg):
        print("找不到 EndNote 库。用法：python tools/verify_reload.py <库文件.enl>")
        print("（或先设置环境变量 ENDNOTE_LIBRARY）")
        return 2
    sdb_source = source_sdb_path(library_arg)
    print("真实库：", library_arg)
    print("实时数据库：", sdb_source)
    root = tempfile.mkdtemp(prefix="en2ima_reload_")
    enl, db = make_test_library(root, sdb_source)
    print("副本：", enl)

    # ---------- baseline ----------
    lib = EndNoteLibrary(enl)
    base_count = len(lib.active_references)
    group_name = TEST_GROUP
    if group_name:
        group = next((g for g in lib.custom_groups if g.name == group_name), None)
    else:
        group = max(lib.custom_groups, key=lambda g: len(g.member_ids), default=None)
    if group is None:
        print("test library has no usable group")
        lib.close()
        return 2
    group_name = group.name
    gid = group.id
    base_group = lib.refs_in_group(gid)
    print("基线：%d 条文献；分组「%s」%d 篇" % (base_count, group_name, len(base_group)))
    lib.close()

    # ---------- simulate EndNote syncing 1 new record, adding it to the group ----------
    con = writable_connection(db)
    new_id = 900001
    add_reference(con, new_id, "同步测试：新文献一", "测试作者, T.", "2030")
    members = group_ids(con, gid) + [new_id]
    con.execute("UPDATE groups SET members=? WHERE group_id=?", (members_blob(members), gid))
    # and a brand-new group with another new record
    new_id2 = 900002
    add_reference(con, new_id2, "同步测试：新文献二", "测试作者, T.", "2031")
    spec = (
        '<?xml version="1.0" encoding="UTF-8"?><group version="1"><ids>'
        "<id>11111111-2222-3333-4444-555555555555</id><name>同步测试新组</name></ids>"
        '<times><created format="UTC">1795417447</created>'
        '<modified format="UTC">1795417447</modified></times>'
        "<rules><rule>TYPE;3</rule></rules></group>"
    )
    con.execute(
        "INSERT INTO groups (group_id, spec, members) VALUES (?, ?, ?)",
        (900, spec.encode("utf-8"), members_blob([new_id2])),
    )
    con.commit()
    con.close()
    print("已向副本写入 2 条新题录、把新文献一加入「%s」、并新建分组「同步测试新组」" % group_name)

    # ---------- "重新加载" = 关掉重开 ----------
    lib = EndNoteLibrary(enl)
    after_count = len(lib.active_references)
    names = {g.name for g in lib.custom_groups}
    group_refs = lib.refs_in_group(gid)
    new_group = next((g for g in lib.custom_groups if g.name == "同步测试新组"), None)
    hit = [r for r in lib.active_references if "同步测试" in r.title]
    checks = [
        ("文献数 +2", after_count == base_count + 2),
        ("搜到新题录（2 条）", len(hit) == 2),
        ("新题录字段正确", hit and hit[0].title.startswith("同步测试") and hit[0].year in ("2030", "2031")),
        ("分组「%s」人数由 %d 变 %d" % (group_name, len(base_group), len(group_refs)),
         len(group_refs) == len(base_group) + 1),
        ("新成员确实在分组里", new_id in [r.id for r in group_refs]),
        ("出现新分组", new_group is not None and "同步测试新组" in names),
        ("新分组里有 1 篇", new_group is not None and len(lib.refs_in_group(new_group.id)) == 1),
    ]
    print("\n== 重新加载后的数据核对 ==")
    for label, ok in checks:
        print(("  ✅ " if ok else "  ❌ ") + label)
    lib.close()

    # ---------- GUI 层：用真实 App 的 reload_libraries() 再验证一次 ----------
    print("\n== 界面层：点「重新加载」按钮的行为 ==")
    gui_ok = True
    try:
        import tkinter as tk

        from app import App, enable_dpi_awareness

        enable_dpi_awareness()
        gui_root = tk.Tk()
        gui_root.withdraw()
        app = App(gui_root)
        app.add_library_path(enl)
        gui_root.update()
        path = os.path.abspath(enl)
        gui_lib = app.libs[path]
        before = len(gui_lib.active_references)
        gui_gid = next(g.id for g in gui_lib.custom_groups if g.name == group_name)
        app.show_view(path, "group", gui_gid)
        gui_root.update()
        app.select_all_visible()
        gui_root.update()
        selected_before = len(app.sel[path])
        tree_label = app.tree.item(
            next(i for i, m in app.node_meta.items() if m == (path, "group", gui_gid))
        )["text"]
        print("重载前：界面显示 %r，列表 %d 行，选中 %d 篇"
              % (tree_label, len(app.row_meta), selected_before))

        # EndNote 又同步了 1 条，并加入同一分组
        con = writable_connection(db)
        new_id3 = 900003
        add_reference(con, new_id3, "同步测试：新文献三", "测试作者, T.", "2032")
        members = group_ids(con, gui_gid) + [new_id3]
        con.execute("UPDATE groups SET members=? WHERE group_id=?", (members_blob(members), gui_gid))
        con.commit()
        con.close()

        app.reload_libraries()
        gui_root.update()
        gui_lib = app.libs[path]
        after = len(gui_lib.active_references)
        rows = {ref.id for _, ref in app.row_meta.values()}
        new_node = next((i for i, m in app.node_meta.items() if m == (path, "group", gui_gid)), None)
        label_after = app.tree.item(new_node)["text"] if new_node else "(视图丢失)"
        checks2 = [
            ("文献数 +1（%d → %d）" % (before, after), after == before + 1),
            ("仍停留在原分组视图", new_node is not None and app.current_path == path),
            ("分组标签计数更新（%r）" % label_after,
             new_node is not None and label_after.endswith("（%d）" % (len(gui_lib.refs_in_group(gui_gid))))),
            ("列表里出现新文献三", new_id3 in rows),
            ("原有选中项保留（%d 篇）" % selected_before,
             selected_before > 0 and len(app.sel[path]) == selected_before),
        ]
        for label, ok in checks2:
            print(("  ✅ " if ok else "  ❌ ") + label)
            gui_ok = gui_ok and ok
        app.on_close()
    except Exception as exc:  # pragma: no cover
        import traceback
        traceback.print_exc()
        gui_ok = False

    all_ok = all(ok for _, ok in checks) and gui_ok
    print("\n结果：", "全部通过 ✅" if all_ok else "有失败 ❌")
    shutil.rmtree(root, ignore_errors=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
