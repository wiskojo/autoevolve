import asyncio

from rich.text import Text
from textual.content import Content
from textual.css.query import NoMatches
from textual.widgets import DataTable, Markdown, Static, TabbedContent

from autoevolve.dashboard import (
    DashboardApp,
    DashboardHeader,
    ExperimentsPane,
    ExperimentTreePane,
    _frontier_chart,
    load_dashboard_snapshot,
)
from tests.e2e.conftest import RepoFixture


def _plain(value: object) -> str:
    if isinstance(value, Text):
        return value.plain
    if isinstance(value, Content):
        return value.plain
    return str(value)


def test_dashboard_snapshot(history_repo: RepoFixture) -> None:
    snapshot = load_dashboard_snapshot(history_repo.root)
    assert snapshot.metric == "benchmark_score"
    assert snapshot.direction == "max"
    assert snapshot.records_count == 12
    assert snapshot.ongoing_count == 0
    assert snapshot.entries
    assert snapshot.frontier
    assert not snapshot.frontier[0].improved
    assert sum(point.improved for point in snapshot.frontier) == snapshot.improvement_count
    chart = _frontier_chart(snapshot, width=120, height=10)
    assert chart[-2].plain.count("▴") == snapshot.improvement_count
    selected_chart = _frontier_chart(
        snapshot,
        width=120,
        height=10,
        selected_sha=snapshot.entries[-1].sha,
    )
    assert any("│" in line.plain for line in selected_chart[:-2])


def test_dashboard_selection_syncs_tree_and_table(history_repo: RepoFixture) -> None:
    async def run() -> None:
        app = DashboardApp(cwd=history_repo.root, refresh_interval=0)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            header = app.query_one(DashboardHeader)
            header_text = _plain(header.render())
            assert "-" in header_text and ":" in header_text

            table = app.query_one(ExperimentsPane)
            tree = app.query_one(ExperimentTreePane)
            assert table.row_count == 12
            assert table.selected_key == app.selected_key
            assert tree.cursor_node is not None
            assert tree.cursor_node.data == app.selected_key

            table.focus()
            await pilot.press("down")
            await pilot.pause()
            assert table.selected_key == app.selected_key
            assert tree.cursor_node is not None
            assert tree.cursor_node.data == app.selected_key

            tree.focus()
            await pilot.press("down")
            await pilot.pause()
            assert tree.cursor_node is not None
            assert tree.cursor_node.data == app.selected_key
            assert table.selected_key == app.selected_key

            await pilot.press("enter")
            await pilot.pause()
            assert tree.cursor_node is not None
            assert tree.cursor_node.data == app.selected_key
            assert table.selected_key == app.selected_key

            selected_before_refresh = app.selected_key
            tree_before_refresh = tree.cursor_node.data
            app._refresh()
            await pilot.pause()
            assert app.selected_key == selected_before_refresh
            assert table.selected_key == selected_before_refresh
            assert tree.cursor_node is not None
            assert tree.cursor_node.data == tree_before_refresh

            await pilot.resize_terminal(110, 32)
            await pilot.pause()

            table.focus()
            await pilot.press("down")
            await pilot.pause()
            assert table.selected_key == app.selected_key
            assert tree.cursor_node is not None
            assert tree.cursor_node.data == app.selected_key

            tree.focus()
            await pilot.press("down")
            await pilot.pause()
            assert tree.cursor_node is not None
            assert tree.cursor_node.data == app.selected_key
            assert table.selected_key == app.selected_key

    asyncio.run(run())


def test_dashboard_enter_opens_detail_modal(history_repo: RepoFixture) -> None:
    async def run() -> None:
        app = DashboardApp(cwd=history_repo.root, refresh_interval=0)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            table = app.query_one(ExperimentsPane)
            table.focus()

            await pilot.press("enter")
            await pilot.pause()

            assert len(app.screen_stack) == 2
            tabs = app.screen.query_one("#detail-tabs", TabbedContent)
            assert tabs.active == "detail-experiment"
            selected = next(entry for entry in app.snapshot.entries if entry.key == app.selected_key)

            header = app.screen.query_one("#detail-header", Static)
            text = _plain(header.render())
            assert f"#{selected.number}" in text
            assert selected.sha[:7] in text

            detail = app.screen.query_one("#detail-experiment-content", Static)
            text = _plain(detail.render())
            assert "SUMMARY" in text
            assert "METRICS" in text
            assert "REFERENCES" in text

            journal = app.screen.query_one("#detail-journal-content", Markdown)
            assert journal is not None

            tabs.active = "detail-lineage"
            await pilot.pause()
            lineage = app.screen.query_one("#detail-lineage-content", Static)
            text = _plain(lineage.render())
            assert f"#{selected.number}" in text
            assert selected.sha[:7] in text

            tabs.active = "detail-code"
            await pilot.pause()
            summary = app.screen.query_one("#detail-code-summary", Static)
            text = _plain(summary.render())
            assert "BASE" in text
            assert "SUMMARY" in text
            assert "FILES" in text

            files = app.screen.query_one("#detail-code-files", DataTable)
            assert files.row_count >= 0

            code = app.screen.query_one("#detail-code-content", Static)
            assert _plain(code.render())

            await pilot.press("escape")
            await pilot.pause()

            assert len(app.screen_stack) == 1
            try:
                app.screen.query_one("#detail-experiment-content", Static)
            except NoMatches:
                return
            raise AssertionError("detail modal should close on escape")

    asyncio.run(run())


def test_dashboard_double_click_opens_and_backdrop_click_closes(history_repo: RepoFixture) -> None:
    async def run() -> None:
        app = DashboardApp(cwd=history_repo.root, refresh_interval=0)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            table = app.query_one(ExperimentsPane)
            tree = app.query_one(ExperimentTreePane)

            await pilot.double_click(table, offset=(8, 3))
            await pilot.pause()
            assert len(app.screen_stack) == 2

            await pilot.click(app.screen, offset=(1, 1))
            await pilot.pause()
            assert len(app.screen_stack) == 1

            await pilot.double_click(tree, offset=(6, 2))
            await pilot.pause()
            assert len(app.screen_stack) == 2

    asyncio.run(run())


def test_dashboard_refresh_runs_async_without_overlap(history_repo: RepoFixture) -> None:
    async def run() -> None:
        app = DashboardApp(cwd=history_repo.root, refresh_interval=0)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._refresh()
            first = app._refresh_worker
            assert first is not None

            app._refresh()
            assert app._refresh_worker is first

            while app._refresh_worker is not None:
                await pilot.pause()

    asyncio.run(run())


def test_dashboard_ongoing_rows_and_tree_attachment(
    history_repo_with_ongoing: RepoFixture,
) -> None:
    snapshot = load_dashboard_snapshot(history_repo_with_ongoing.root)
    assert snapshot.ongoing_count == 2
    assert [entry.ref for entry in snapshot.ongoing] == ["alpha-branch", "main-fork"]

    recorded_parent = history_repo_with_ongoing.git("rev-parse", "cross/hybrid-final").strip()
    alpha = next(entry for entry in snapshot.ongoing if entry.ref == "alpha-branch")
    orphan = next(entry for entry in snapshot.ongoing if entry.ref == "main-fork")
    assert alpha.parent_key == recorded_parent
    assert orphan.parent_key is None
    assert alpha.summary == "Alpha branch is exploring the strongest recorded lineage."
    assert orphan.summary == "Main fork is still defining its first real experiment."

    async def run() -> None:
        app = DashboardApp(cwd=history_repo_with_ongoing.root, refresh_interval=0)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            table = app.query_one(ExperimentsPane)
            tree = app.query_one(ExperimentTreePane)

            assert table.row_count == 14
            assert "SHA" in [_plain(column.label) for column in table.columns.values()]

            top_row = table.get_row_at(0)
            second_row = table.get_row_at(1)
            first_recorded = table.get_row_at(2)
            assert _plain(top_row[0]) == "14"
            assert _plain(top_row[1]) == "--"
            assert _plain(top_row[2]).startswith("alpha-branch:")
            assert "Alpha branch is explor" in _plain(top_row[2])
            assert _plain(top_row[3]) == "pending"
            assert _plain(top_row[4]) == "--"
            assert _plain(top_row[5]) == "--"
            assert _plain(second_row[0]) == "13"
            assert _plain(second_row[1]) == "--"
            assert _plain(second_row[2]).startswith("main-fork:")
            assert "Main fork is still defin" in _plain(second_row[2])
            assert _plain(second_row[3]) == "pending"
            assert _plain(second_row[4]) == "--"
            assert _plain(second_row[5]) == "--"
            assert _plain(first_recorded[0]) == "12"
            assert len(_plain(first_recorded[1])) == 7

            table.focus()
            table.move_cursor(row=0, column=0, animate=False)
            await pilot.pause()
            assert app.selected_key == alpha.key
            assert table.selected_key == alpha.key
            assert app._selected_record_sha() is None
            assert tree.cursor_node is not None
            assert tree.cursor_node.data == alpha.key
            alpha_parent = tree._node_by_key[alpha.key].parent
            assert alpha_parent is not None
            assert alpha_parent.data == recorded_parent

            await pilot.press("enter")
            await pilot.pause()
            assert len(app.screen_stack) == 1

            table.move_cursor(row=1, column=0, animate=False)
            await pilot.pause()
            assert app.selected_key == orphan.key
            assert tree.cursor_node is not None
            assert tree.cursor_node.data == orphan.key
            orphan_parent = tree._node_by_key[orphan.key].parent
            assert orphan_parent is not None
            assert orphan_parent.data is None

            await pilot.double_click(table, offset=(8, 2))
            await pilot.pause()
            assert len(app.screen_stack) == 1

    asyncio.run(run())


def test_dashboard_ongoing_summary_fallbacks(history_repo: RepoFixture) -> None:
    history_repo.run(
        "start",
        "broken-ongoing",
        "Broken ongoing branch for placeholder coverage.",
        "--from",
        "main",
    )
    worktree = history_repo.managed_worktree_path("broken-ongoing")

    (worktree / "EXPERIMENT.json").write_text("{\n", encoding="utf-8")
    snapshot = load_dashboard_snapshot(history_repo.root)
    broken = next(entry for entry in snapshot.ongoing if entry.ref == "broken-ongoing")
    assert broken.summary == "(invalid EXPERIMENT.json)"

    (worktree / "EXPERIMENT.json").unlink()
    snapshot = load_dashboard_snapshot(history_repo.root)
    broken = next(entry for entry in snapshot.ongoing if entry.ref == "broken-ongoing")
    assert broken.summary == "(missing EXPERIMENT.json)"
