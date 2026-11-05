"""备份导出、导入与平台改名：三层作用域迁移。"""

from __future__ import annotations

import pytest

from support import pkg_memstore, pkg_registry, run, thing


def make(tmp_path):
    from support import import_core

    transfer = import_core("transfer")
    store = pkg_memstore.MemoryStore(tmp_path / "memories")
    registry = pkg_registry.SessionRegistry(tmp_path / "sessions.json")
    return transfer, transfer.MemoryTransfer(store, registry), store, registry


def test_make_ref_rejects_removed_scopes(tmp_path):
    transfer, _, _, _ = make(tmp_path)
    for scope in ("global", "session", "people"):
        with pytest.raises(transfer.TransferError):
            transfer.make_ref(scope, "x")
    assert transfer.make_ref("board").key == ""
    assert transfer.make_ref("notebook", "Bot-A").key == "Bot-A"
    key = pkg_memstore.safe_key("Bot-A:FriendMessage:1")
    assert transfer.make_ref("whisper", key).key == key


def test_export_and_import_three_layers(tmp_path):
    transfer, mover, store, _ = make(tmp_path)
    run(store.board_scope().write("notice.md", thing("notice", body="告示")))
    run(store.notebook_scope("Bot-A").write("note.md", thing("note", body="小本本")))
    run(store.whisper_scope("a").write("secret.md", thing("secret", body="悄悄话")))
    bundle = run(mover.export())
    scopes = {item["scope"]: item for item in bundle["scopes"]}
    assert set(scopes) == {"board", "notebook", "whisper"}
    assert bundle["format"] == transfer.BUNDLE_FORMAT

    parsed = transfer.parse_bundle(bundle)
    reports = run(mover.import_bundle(parsed, dry_run=True))
    assert all(report["counts"]["same"] == 1 for report in reports)


def test_import_whisper_lands_only_in_selected_key(tmp_path):
    transfer, mover, store, _ = make(tmp_path)
    run(store.whisper_scope("a").write("secret.md", thing("secret", body="A 的秘密")))
    bundle = run(mover.export([transfer.ScopeRef("whisper", "a")]))
    parsed = transfer.parse_bundle(bundle)
    # 目标选另一段对话：管理员显式导入，内容只落在 b
    parsed[0] = transfer.BundleScope(
        ref=transfer.ScopeRef("whisper", "b"), entries=parsed[0].entries
    )
    run(mover.import_bundle(parsed))
    assert run(store.whisper_scope("b").read("secret.md"))
    assert "A 的秘密" in run(store.whisper_scope("b").read("secret.md"))
    assert run(store.whisper_scope("c").read("secret.md")) is None


def test_scope_transfer_copy_and_move(tmp_path):
    transfer, mover, store, _ = make(tmp_path)
    source = store.whisper_scope("a")
    run(source.write("secret.md", thing("secret", body="内容")))
    report = run(
        mover.transfer(
            transfer.ScopeRef("whisper", "a"),
            transfer.ScopeRef("whisper", "b"),
            mode=transfer.MODE_COPY,
        )
    )
    assert report["counts"]["added"] == 1
    assert run(source.read("secret.md"))
    assert run(store.whisper_scope("b").read("secret.md"))

    report = run(
        mover.transfer(
            transfer.ScopeRef("whisper", "b"),
            transfer.ScopeRef("whisper", "c"),
            mode=transfer.MODE_MOVE,
        )
    )
    assert report["moved"] == 1
    assert run(store.whisper_scope("b").read("secret.md")) is None
    assert run(store.whisper_scope("c").read("secret.md"))


def test_platform_rename_moves_notebook_and_whispers(tmp_path):
    transfer, mover, store, registry = make(tmp_path)
    run(store.notebook_scope("Bot-A").write("note.md", thing("note", body="小本本")))
    umo = "Bot-A:FriendMessage:123"
    run(store.whisper_scope(pkg_memstore.safe_key(umo)).write(
        "secret.md", thing("secret", body="悄悄话")
    ))
    registry.adopt(pkg_memstore.safe_key(umo), umo)

    result = run(mover.remap_platform("Bot-A", "Bot-B", mode=transfer.MODE_MOVE))
    assert result["items"]
    assert run(store.notebook_scope("Bot-B").read("note.md"))
    assert run(store.notebook_scope("Bot-A").read("note.md")) is None
    new_key = pkg_memstore.safe_key("Bot-B:FriendMessage:123")
    assert run(store.whisper_scope(new_key).read("secret.md"))
    assert run(store.whisper_scope(pkg_memstore.safe_key(umo)).read("secret.md")) is None


def test_platform_rename_can_skip_notebook(tmp_path):
    transfer, mover, store, _ = make(tmp_path)
    run(store.notebook_scope("Bot-A").write("note.md", thing("note", body="小本本")))
    run(mover.remap_platform("Bot-A", "Bot-B", include_notebook=False))
    assert run(store.notebook_scope("Bot-A").read("note.md"))
    assert run(store.notebook_scope("Bot-B").read("note.md")) is None
