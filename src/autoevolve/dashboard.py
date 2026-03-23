import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rich.cells import cell_len
from rich.console import Group
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.events import Resize
from textual.geometry import Size
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import (
    DataTable,
    Markdown,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)
from textual.widgets._tree import TreeNode
from textual.worker import Worker, WorkerState

from autoevolve.git import diff
from autoevolve.models.experiment import ExperimentIndexEntry
from autoevolve.models.git import GitChangedPath, GitDiff
from autoevolve.models.lineage import LineageEdge
from autoevolve.models.types import GraphDirection, GraphEdges, MetricDirection
from autoevolve.repository import (
    EXPERIMENT_FILE,
    JOURNAL_FILE,
    ExperimentRepository,
    parse_experiment_document,
)

_BRAILLE_BITS = {
    (0, 0): 0x01,
    (0, 1): 0x02,
    (0, 2): 0x04,
    (0, 3): 0x40,
    (1, 0): 0x08,
    (1, 1): 0x10,
    (1, 2): 0x20,
    (1, 3): 0x80,
}

_POSITIVE_COLOR = "color(78)"
_NEGATIVE_COLOR = "color(204)"


@dataclass(frozen=True)
class DashboardEntry:
    key: str
    number: int
    sha: str
    ref: str
    summary: str
    score: float
    delta: float | None
    age: str
    timestamp: datetime
    parent_key: str | None
    improved: bool


@dataclass(frozen=True)
class FrontierPoint:
    timestamp: datetime
    score: float
    frontier: float
    improved: bool


@dataclass(frozen=True)
class OngoingEntry:
    key: str
    number: int
    ref: str
    summary: str
    path: Path
    branch: str | None
    head: str
    parent_key: str | None
    score: float | None = None
    delta: float | None = None
    age: str = ""


DashboardRow = DashboardEntry | OngoingEntry


@dataclass(frozen=True)
class DashboardSnapshot:
    root_path: Path
    metric: str
    direction: MetricDirection
    status_message: str | None
    records_count: int
    ongoing_count: int
    improvement_count: int
    best_sha: str
    best_score: float
    best_age: str
    latest_sha: str
    latest_summary: str
    latest_age: str
    entries: tuple[DashboardEntry, ...]
    ongoing: tuple[OngoingEntry, ...]
    frontier: tuple[FrontierPoint, ...]


@dataclass(frozen=True)
class CodeChangeFile:
    path: str
    status: str
    display_path: str
    additions: int
    deletions: int
    diff: Text


@dataclass(frozen=True)
class CodeChangesView:
    summary: Text
    files: tuple[CodeChangeFile, ...]


def _empty_snapshot(
    root_path: Path,
    *,
    message: str,
    metric: str = "",
    direction: MetricDirection = "max",
    records_count: int = 0,
    ongoing: list[OngoingEntry] | None = None,
) -> DashboardSnapshot:
    ongoing_rows = ongoing or []
    numbered_ongoing = [
        OngoingEntry(
            key=entry.key,
            number=len(ongoing_rows) - index,
            ref=entry.ref,
            summary=entry.summary,
            path=entry.path,
            branch=entry.branch,
            head=entry.head,
            parent_key=entry.parent_key,
            score=entry.score,
            delta=entry.delta,
            age=entry.age,
        )
        for index, entry in enumerate(ongoing_rows)
    ]
    return DashboardSnapshot(
        root_path=root_path.resolve(),
        metric=metric,
        direction=direction,
        status_message=message,
        records_count=records_count,
        ongoing_count=len(numbered_ongoing),
        improvement_count=0,
        best_sha="",
        best_score=0.0,
        best_age="",
        latest_sha="",
        latest_summary="",
        latest_age="",
        entries=(),
        ongoing=tuple(numbered_ongoing),
        frontier=(),
    )


class ExperimentDetailScreen(ModalScreen[None]):
    CSS = """
    ExperimentDetailScreen {
        align: center middle;
        background: rgba(2, 3, 5, 0.72);
    }

    #detail-modal {
        width: 92%;
        height: 88%;
        border: solid #2a3038;
        background: #0d0f13;
        padding: 0 1 1 1;
    }

    #detail-header {
        height: auto;
        margin-bottom: 0;
        color: #f3f4f6;
    }

    #detail-tabs {
        height: 1fr;
        margin-top: 1;
    }

    #detail-tabs > ContentTabs {
        height: 2;
        background: #111318;
        border-bottom: solid #2a3038;
    }

    #detail-tabs Tab {
        color: #9aa4b2;
        background: #111318;
        padding: 0 1;
    }

    #detail-tabs Tab.-active {
        color: #f3f4f6;
        background: #1a1f27;
        text-style: bold;
    }

    #detail-tabs .underline--bar {
        background: #5ee9b5;
    }

    #detail-tabs TabPane {
        height: 1fr;
        padding: 0;
    }

    #detail-experiment-layout {
        height: 1fr;
    }

    #detail-experiment-pane {
        width: 1fr;
        height: 1fr;
        border: solid #2a3038;
        background: #101318;
        margin-right: 1;
    }

    #detail-journal-pane {
        width: 3fr;
        height: 1fr;
        border: solid #2a3038;
        background: #101318;
    }

    #detail-lineage-pane, #detail-code-pane {
        height: 1fr;
        border: solid #2a3038;
        background: #101318;
    }

    #detail-code-layout {
        height: 1fr;
    }

    #detail-code-summary-pane {
        width: 1fr;
        min-width: 28;
        height: 1fr;
        border: solid #2a3038;
        background: #101318;
        margin-right: 1;
    }

    #detail-code-diff-pane {
        width: 3fr;
        height: 1fr;
        border: solid #2a3038;
        background: #101318;
    }

    .detail-pane-title {
        height: 1;
        padding: 0 1;
        color: #9aa4b2;
        background: #111318;
        text-style: bold;
    }

    .detail-scroll {
        height: 1fr;
        padding: 1 2 1 1;
        scrollbar-background: #111318;
        scrollbar-background-hover: #161a20;
        scrollbar-background-active: #161a20;
        scrollbar-color: #334155;
        scrollbar-color-hover: #475569;
        scrollbar-color-active: #475569;
        scrollbar-corner-color: #0d0f13;
        scrollbar-size-vertical: 1;
    }

    .detail-content {
        color: #f3f4f6;
        padding-right: 1;
    }

    #detail-journal-content {
        color: #f3f4f6;
        padding: 0 1 0 0;
    }

    #detail-lineage-content {
        width: auto;
        color: #f3f4f6;
        text-wrap: nowrap;
        text-overflow: clip;
    }

    #detail-lineage-scroll {
        height: 1fr;
        padding: 1 1 1 1;
        overflow-x: auto;
        overflow-y: auto;
        scrollbar-size-horizontal: 0;
        scrollbar-size-vertical: 0;
    }

    #detail-code-summary {
        height: auto;
        padding: 1 1 1 1;
        color: #f3f4f6;
        border-bottom: solid #2a3038;
    }

    #detail-code-files {
        height: 1fr;
        color: #f3f4f6;
        background: #101318;
        scrollbar-background: #111318;
        scrollbar-background-hover: #161a20;
        scrollbar-background-active: #161a20;
        scrollbar-color: #334155;
        scrollbar-color-hover: #475569;
        scrollbar-color-active: #475569;
        scrollbar-corner-color: #0d0f13;
        scrollbar-size-vertical: 1;
    }

    #detail-code-files > .datatable--header {
        background: #14171d;
        color: #d6dbe3;
        text-style: bold;
        background-tint: transparent;
    }

    #detail-code-files > .datatable--header-cursor {
        background: #14171d;
        color: #d6dbe3;
        text-style: bold;
    }

    #detail-code-files > .datatable--header-hover {
        background: #14171d;
        color: #d6dbe3;
    }

    #detail-code-files > .datatable--cursor {
        background: #252a33;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
    ]

    def __init__(self, cwd: str | Path, entry: DashboardEntry):
        super().__init__()
        self._title = _detail_title(entry)
        repository = ExperimentRepository(cwd)
        record = repository.resolve_index(entry.sha)
        detail = repository.detail(record.sha)
        previous = repository.previous_record(record)
        base = (
            previous.sha
            if previous is not None
            else (record.parents[0] if record.parents else None)
        )
        patch = (
            diff(
                repository.repo,
                base,
                record.sha,
                exclude=(EXPERIMENT_FILE, JOURNAL_FILE),
            )
            if base is not None
            else None
        )
        self._experiment = _experiment_summary_text(record)
        self._journal = detail.journal
        self._code = _code_changes_view(base, previous is not None, patch)
        self._selected_code_path = self._code.files[0].path if self._code.files else None
        self._lineage, self._lineage_line = _combined_lineage_text(repository, record)

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-modal"):
            yield Static(self._title, id="detail-header")
            with TabbedContent(initial="detail-experiment", id="detail-tabs"):
                with TabPane("Experiment", id="detail-experiment"):
                    with Horizontal(id="detail-experiment-layout"):
                        with Vertical(id="detail-experiment-pane"):
                            yield Static("EXPERIMENT", classes="detail-pane-title")
                            with ScrollableContainer(classes="detail-scroll"):
                                yield Static(
                                    self._experiment,
                                    id="detail-experiment-content",
                                    classes="detail-content",
                                )
                        with Vertical(id="detail-journal-pane"):
                            yield Static("JOURNAL", classes="detail-pane-title")
                            with ScrollableContainer(classes="detail-scroll"):
                                yield Markdown(
                                    self._journal,
                                    id="detail-journal-content",
                                    open_links=False,
                                )
                with TabPane("Lineage", id="detail-lineage"):
                    with Vertical(id="detail-lineage-pane"):
                        yield Static("LINEAGE", classes="detail-pane-title")
                        with ScrollableContainer(id="detail-lineage-scroll"):
                            yield PreformattedText(
                                self._lineage,
                                id="detail-lineage-content",
                                classes="detail-content",
                            )
                with TabPane("Code Changes", id="detail-code"):
                    with Horizontal(id="detail-code-layout"):
                        with Vertical(id="detail-code-summary-pane"):
                            yield Static("CODE CHANGES", classes="detail-pane-title")
                            yield Static(self._code.summary, id="detail-code-summary")
                            yield CodeFilesPane(id="detail-code-files")
                        with Vertical(id="detail-code-diff-pane"):
                            yield Static(
                                "DIFF", id="detail-code-diff-title", classes="detail-pane-title"
                            )
                            with ScrollableContainer(classes="detail-scroll"):
                                yield Static(
                                    self._selected_code_diff(),
                                    id="detail-code-content",
                                    classes="detail-content",
                                )

    def action_close(self) -> None:
        self.dismiss()

    def on_mount(self) -> None:
        self.query_one(CodeFilesPane).set_view(self._code, self._selected_code_path)
        self._refresh_selected_code_diff()
        self.call_after_refresh(self._center_lineage)

    def on_click(self, event: events.Click) -> None:
        if event.widget is self:
            self.dismiss()
            event.stop()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "detail-code-files":
            return
        if isinstance(event.row_key.value, str):
            self._set_selected_code_path(event.row_key.value)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "detail-code-files":
            return
        if isinstance(event.row_key.value, str):
            self._set_selected_code_path(event.row_key.value)

    def _center_lineage(self) -> None:
        scroll = self.query_one("#detail-lineage-scroll", ScrollableContainer)
        viewport_height = max(scroll.content_region.height, 1)
        target_y = max(self._lineage_line - viewport_height // 2, 0)
        scroll.scroll_to(y=target_y, animate=False, immediate=True)

    def _selected_code_diff(self) -> Text:
        if self._selected_code_path is None:
            return Text("(none)", style="#8b95a7")
        for item in self._code.files:
            if item.path == self._selected_code_path:
                return item.diff
        return Text("(none)", style="#8b95a7")

    def _set_selected_code_path(self, path: str) -> None:
        if path == self._selected_code_path:
            return
        self._selected_code_path = path
        self._refresh_selected_code_diff()

    def _refresh_selected_code_diff(self) -> None:
        title = self.query_one("#detail-code-diff-title", Static)
        content = self.query_one("#detail-code-content", Static)
        selected = next(
            (item for item in self._code.files if item.path == self._selected_code_path), None
        )
        title.update(selected.display_path if selected is not None else "DIFF")
        content.update(selected.diff if selected is not None else Text("(none)", style="#8b95a7"))


class DashboardHeader(Static):
    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)
        self._snapshot: DashboardSnapshot | None = None
        self._refreshed_at: datetime | None = None

    def set_snapshot(self, snapshot: DashboardSnapshot, refreshed_at: datetime) -> None:
        self._snapshot = snapshot
        self._refreshed_at = refreshed_at
        self._refresh_view()

    def on_resize(self) -> None:
        if self._snapshot is not None and self._refreshed_at is not None:
            self._refresh_view()

    def _refresh_view(self) -> None:
        if self._snapshot is None or self._refreshed_at is None:
            return
        snapshot = self._snapshot
        refreshed = self._refreshed_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        left = Text()
        left.append(" autoevolve ", style="bold #f3f4f6 on #1c2128")
        left.append(f" {snapshot.root_path.name}", style="bold #e5e7eb")
        if snapshot.metric:
            left.append(f"  {snapshot.direction} {snapshot.metric}", style="#a8b0bc")
        right = Text(refreshed, style="#8b95a7")

        width = max(self.content_region.width, self.size.width)
        padding = max(width - cell_len(left.plain) - cell_len(right.plain), 2)

        text = Text()
        text.append_text(left)
        text.append(" " * padding, style="#111318")
        text.append_text(right)
        self.update(text, layout=False)


def _footer_text() -> Text:
    text = Text()
    text.append("↑↓", style="#d6dbe3")
    text.append(" Select  ", style="#a8b0bc")
    text.append("←→", style="#d6dbe3")
    text.append(" Scroll  ", style="#a8b0bc")
    text.append("Enter", style="#d6dbe3")
    text.append(" Details  ", style="#a8b0bc")
    text.append("q", style="#d6dbe3")
    text.append(" Quit", style="#a8b0bc")
    return text


class PreformattedText(Static):
    def get_content_width(self, container: Size, viewport: Size) -> int:
        renderable = self.render()
        plain = renderable.plain if isinstance(renderable, Text) else str(renderable)
        return max((cell_len(line) for line in plain.splitlines()), default=0)

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        renderable = self.render()
        plain = renderable.plain if isinstance(renderable, Text) else str(renderable)
        return max(len(plain.splitlines()), 1)


class ExperimentTreePane(Tree[str | None]):
    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__("experiments", name=name, id=id, classes=classes, disabled=disabled)
        self.show_root = False
        self.guide_depth = 4
        self.show_horizontal_scrollbar = False
        self.show_vertical_scrollbar = False
        self.border_title = Text("Tree", style="bold #9aa4b2")
        self._node_by_key: dict[str, TreeNode[str | None]] = {}

    BINDINGS = [Binding("enter", "open_detail", show=False)]

    def set_snapshot(self, snapshot: DashboardSnapshot, selected_key: str) -> None:
        self.reset("experiments")
        self.show_root = False
        self._node_by_key = {}
        children_by_parent: dict[str | None, list[DashboardRow]] = {}
        for entry in snapshot.entries:
            children_by_parent.setdefault(entry.parent_key, []).append(entry)
        for ongoing in snapshot.ongoing:
            children_by_parent.setdefault(ongoing.parent_key, []).append(ongoing)
        for children in children_by_parent.values():
            children.sort(key=_tree_sort_key)

        def add_children(parent: TreeNode[str | None], parent_key: str | None) -> None:
            for entry in children_by_parent.get(parent_key, []):
                node = parent.add(
                    _tree_label(entry), data=entry.key, expand=True, allow_expand=False
                )
                self._node_by_key[entry.key] = node
                add_children(node, entry.key)

        add_children(self.root, None)
        self.root.expand_all()
        self.select_key(selected_key)

    def select_key(self, key: str, *, center: bool = False) -> None:
        node = self._node_by_key.get(key)
        if node is None:
            return
        previous_center_scroll = self.center_scroll
        self.center_scroll = center
        try:
            self.move_cursor(node, animate=False)
        finally:
            self.center_scroll = previous_center_scroll
        self._center_node_x(node)

    def action_toggle_node(self) -> None:
        return

    def action_open_detail(self) -> None:
        app = self.app
        if isinstance(app, DashboardApp):
            app.open_detail_for_selected()

    async def _on_click(self, event: events.Click) -> None:
        await super()._on_click(event)
        node = self.cursor_node
        if event.chain == 2 and node is not None and isinstance(node.data, str):
            app = self.app
            if isinstance(app, DashboardApp):
                app.open_detail_for_selected()

    def _center_node_x(self, node: TreeNode[str | None]) -> None:
        label = node.label
        label_text = label.plain if isinstance(label, Text) else label
        x = max(_node_depth(node) * 4 - 2, 0)
        width = max(len(label_text) + 6, 12)
        viewport_width = max(self.scrollable_content_region.width, 1)
        target_x = max(x + width / 2 - viewport_width / 2, 0)
        self.scroll_to(
            x=min(target_x, self.max_scroll_x),
            animate=False,
            immediate=True,
        )


class FrontierPane(Static):
    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)
        self._snapshot: DashboardSnapshot | None = None
        self._selected_sha: str | None = None

    def set_snapshot(self, snapshot: DashboardSnapshot, selected_sha: str | None) -> None:
        self._snapshot = snapshot
        self._selected_sha = selected_sha
        self._refresh_view()

    def on_resize(self) -> None:
        if self._snapshot is not None:
            self._refresh_view()

    def _refresh_view(self) -> None:
        if self._snapshot is None:
            self.update(_title("frontier"), layout=False)
            return
        snapshot = self._snapshot
        if not snapshot.frontier:
            self.update(
                Group(
                    _frontier_header(snapshot),
                    Text(""),
                    Text(snapshot.status_message or "Waiting for experiments.", style="#8b95a7"),
                ),
                layout=False,
            )
            return
        width = max(self.content_region.width, 60)
        height = max(self.content_region.height, 10)
        lines = [
            _frontier_header(snapshot),
            Text(""),
            *(
                _frontier_chart(
                    snapshot,
                    width=width,
                    height=max(height - 2, 7),
                    selected_sha=self._selected_sha,
                )
            ),
        ]
        self.update(Group(*lines), layout=False)


class ExperimentsPane(DataTable[object]):
    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            show_row_labels=False,
            zebra_stripes=False,
            cursor_type="row",
            cell_padding=1,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )
        self.border_title = Text("Experiments", style="bold #9aa4b2")
        self.show_horizontal_scrollbar = False
        self.styles.overflow_x = "hidden"
        self._snapshot: DashboardSnapshot | None = None
        self._rows: list[DashboardRow] = []

    BINDINGS = [Binding("enter", "open_detail", show=False)]

    def set_snapshot(self, snapshot: DashboardSnapshot, selected_key: str) -> None:
        self._snapshot = snapshot
        self._rows = _ordered_dashboard_rows(snapshot)
        self._refresh_view(selected_key)

    def refresh_relative_fields(self, snapshot: DashboardSnapshot, selected_key: str) -> None:
        rows = _ordered_dashboard_rows(snapshot)
        if [entry.key for entry in rows] != [entry.key for entry in self._rows]:
            self.set_snapshot(snapshot, selected_key)
            return
        self._snapshot = snapshot
        self._rows = rows
        for row_index, entry in enumerate(self._rows):
            self.update_cell_at(
                Coordinate(row_index, 5),
                Text(_table_age(entry), style="#d1d5db" if _is_recorded(entry) else "#8b95a7"),
                update_width=True,
            )

    @property
    def selected_key(self) -> str | None:
        if not self.row_count or not self.is_valid_row_index(self.cursor_row):
            return None
        return self._rows[self.cursor_row].key

    def select_key(self, key: str) -> None:
        if not self.row_count:
            return
        try:
            row = next(index for index, entry in enumerate(self._rows) if entry.key == key)
        except StopIteration:
            return
        self.move_cursor(row=row, column=0, animate=False)

    def select_relative(self, offset: int) -> None:
        if not self.row_count:
            return
        row = min(max(self.cursor_row + offset, 0), self.row_count - 1)
        self.move_cursor(row=row, column=0, animate=False)

    def on_resize(self) -> None:
        if self._snapshot is not None:
            self._refresh_view(self.selected_key)

    def watch_cursor_coordinate(
        self,
        old_coordinate: Coordinate,
        new_coordinate: Coordinate,
    ) -> None:
        super().watch_cursor_coordinate(old_coordinate, new_coordinate)
        if old_coordinate == new_coordinate:
            return
        app = self.app
        if isinstance(app, DashboardApp) and not app._syncing_selection and not app._resizing:
            key = self.selected_key
            if key is not None:
                app._set_selected_key(key, source="table")

    def action_open_detail(self) -> None:
        app = self.app
        if isinstance(app, DashboardApp):
            app.open_detail_for_selected()

    async def _on_click(self, event: events.Click) -> None:
        await super()._on_click(event)
        if event.chain == 2 and self.selected_key is not None:
            app = self.app
            if isinstance(app, DashboardApp):
                app.open_detail_for_selected()

    def _refresh_view(self, selected_key: str | None) -> None:
        if self._snapshot is None:
            return
        width = max(self.content_region.width, 32)
        recorded_rows = [entry for entry in self._rows if isinstance(entry, DashboardEntry)]
        ref_width = max([7, len("--"), *(len(entry.ref) for entry in recorded_rows)]) + 1
        score_width = max(
            len("SCORE") + 1,
            *(len(_format_score(entry.score)) + 1 for entry in recorded_rows),
            len(_ongoing_score_placeholder()) + 1,
        )
        delta_width = max(
            12,
            *(len(_format_delta(entry.delta)) + 1 for entry in recorded_rows),
            len(_ongoing_placeholder()) + 1,
        )
        age_width = max(
            10,
            *(len(_table_age(entry)) + 1 for entry in recorded_rows),
            len(_ongoing_placeholder()) + 1,
        )
        summary_width = max(
            width - (4 + ref_width + score_width + delta_width + age_width + 10),
            12,
        )
        self.clear(columns=True)
        self.add_column("#", width=4, key="number")
        self.add_column("SHA", width=ref_width, key="ref")
        self.add_column("SUMMARY", width=summary_width, key="summary")
        self.add_column("SCORE", width=score_width, key="score")
        self.add_column("Δ", width=delta_width, key="delta")
        self.add_column("AGE", width=age_width, key="age")

        for entry in self._rows:
            self.add_row(
                _table_number_cell(entry),
                _table_ref_cell(entry, ref_width),
                _table_summary_cell(entry, summary_width),
                _table_score_cell(entry),
                _table_delta_cell(entry),
                Text(_table_age(entry), style="#d1d5db" if _is_recorded(entry) else "#8b95a7"),
                key=entry.key,
            )

        if self._rows:
            self.select_key(selected_key or self._rows[0].key)


class CodeFilesPane(DataTable[object]):
    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            show_row_labels=False,
            zebra_stripes=False,
            cursor_type="row",
            cell_padding=1,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )
        self.show_horizontal_scrollbar = False
        self.styles.overflow_x = "hidden"
        self._files: tuple[CodeChangeFile, ...] = ()

    def set_view(self, view: CodeChangesView, selected_path: str | None) -> None:
        self._files = view.files
        self._refresh_view(selected_path)

    def _refresh_view(self, selected_path: str | None) -> None:
        width = max(self.size.width - 10, 24)
        path_width = max(width - 16, 12)
        self.clear(columns=True)
        self.add_column("S", width=3, key="status")
        self.add_column("+", width=6, key="additions")
        self.add_column("-", width=6, key="deletions")
        self.add_column("FILE", width=path_width, key="path")
        for item in self._files:
            self.add_row(
                Text(item.status, style="#8b95a7"),
                Text(f"+{item.additions}", style=_POSITIVE_COLOR),
                Text(f"-{item.deletions}", style=_NEGATIVE_COLOR),
                Text(_truncate(item.display_path, path_width), style="#f3f4f6"),
                key=item.path,
            )
        if self._files:
            row_key = selected_path or self._files[0].path
            try:
                row = self.get_row_index(row_key)
            except KeyError:
                row = 0
            self.move_cursor(
                row=row,
                column=0,
                animate=False,
            )


class DashboardApp(App[None]):
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        background: #090a0c;
        color: #e5e7eb;
    }

    #header {
        height: 1;
        background: #111318;
        color: #f3f4f6;
        padding: 0 1;
    }

    #body {
        padding: 0 1 1 1;
        layout: vertical;
        height: 1fr;
    }

    #frontier {
        height: 14;
        border: solid #2a3038;
        background: #0d0f13;
        padding: 0 1 0 1;
        margin-bottom: 0;
    }

    #lower {
        height: 1fr;
    }

    #tree {
        width: 1fr;
        min-width: 28;
        height: 1fr;
        border: solid #2a3038;
        background: #0d0f13;
        margin-right: 1;
        color: #f3f4f6;
        scrollbar-background: #111318;
        scrollbar-background-hover: #161a20;
        scrollbar-background-active: #161a20;
        scrollbar-color: #334155;
        scrollbar-color-hover: #475569;
        scrollbar-color-active: #475569;
        scrollbar-corner-color: #0d0f13;
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 0;
    }

    #experiments {
        width: 3fr;
        height: 1fr;
        border: solid #2a3038;
        background: #0d0f13;
        color: #f3f4f6;
        scrollbar-background: #111318;
        scrollbar-background-hover: #161a20;
        scrollbar-background-active: #161a20;
        scrollbar-color: #334155;
        scrollbar-color-hover: #475569;
        scrollbar-color-active: #475569;
        scrollbar-corner-color: #0d0f13;
        scrollbar-size-vertical: 1;
    }

    #tree > .tree--guides {
        color: #4b5563;
    }

    #tree > .tree--guides-hover {
        color: #b8c1cc;
    }

    #tree > .tree--guides-selected {
        color: #b8c1cc;
    }

    #tree > .tree--cursor {
        background: #252a33;
        color: #b8c1cc;
        text-style: bold;
    }

    #experiments {
        color: #f3f4f6;
    }

    #experiments > .datatable--header {
        background: #14171d;
        color: #d6dbe3;
        text-style: bold;
        background-tint: transparent;
    }

    #experiments > .datatable--header-cursor {
        background: #14171d;
        color: #d6dbe3;
        text-style: bold;
    }

    #experiments > .datatable--header-hover {
        background: #14171d;
        color: #d6dbe3;
    }

    #experiments > .datatable--cursor {
        background: #252a33;
        text-style: bold;
    }

    #experiments > .datatable--fixed-cursor {
        background: #252a33;
    }

    #footer {
        height: 1;
        background: #111318;
        color: #a8b0bc;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("enter", "open_detail", "Details"),
    ]

    def __init__(
        self,
        cwd: str | Path = ".",
        refresh_interval: float = 1,
    ):
        super().__init__()
        self.cwd = cwd
        self.refresh_interval = refresh_interval
        self.snapshot = load_dashboard_snapshot(cwd)
        self._entries_signature = _entries_signature(self.snapshot)
        self.selected_key = _snapshot_selected_key(self.snapshot)
        self._last_refreshed_at = datetime.now().astimezone()
        self._syncing_selection = False
        self._interaction_ready = False
        self._resizing = False
        self._resize_timer: Timer | None = None
        self._refresh_worker: Worker[DashboardSnapshot] | None = None

    def compose(self) -> ComposeResult:
        yield DashboardHeader(id="header")
        with Vertical(id="body"):
            yield FrontierPane(id="frontier")
            with Horizontal(id="lower"):
                yield ExperimentTreePane(id="tree")
                yield ExperimentsPane(id="experiments")
        yield Static(_footer_text(), id="footer")

    def on_mount(self) -> None:
        self._apply_snapshot("ready")
        table = self.query_one(ExperimentsPane)
        table.focus()
        if table.selected_key is not None:
            self._set_selected_key(table.selected_key, source="table")
        self.call_after_refresh(self._enable_interaction)
        if self.refresh_interval > 0:
            self.set_interval(self.refresh_interval, self._refresh)

    def on_resize(self, _: Resize) -> None:
        self._resizing = True
        if self._resize_timer is None:
            self._resize_timer = self.set_timer(0.05, self._finish_resize)
        else:
            self._resize_timer.reset()

    def action_open_detail(self) -> None:
        self.open_detail_for_selected()

    def open_detail_for_selected(self) -> None:
        entry = next(
            (item for item in self.snapshot.entries if item.key == self.selected_key),
            None,
        )
        if entry is None:
            return
        self.push_screen(ExperimentDetailScreen(self.cwd, entry))

    def _refresh(self) -> None:
        if self._refresh_worker is not None and not self._refresh_worker.is_finished:
            return
        self._refresh_worker = self.run_worker(
            self._load_snapshot,
            name="dashboard-refresh",
            group="dashboard-refresh",
            exit_on_error=False,
            thread=True,
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker is not self._refresh_worker:
            return
        if event.state == WorkerState.SUCCESS:
            snapshot = event.worker.result
            if snapshot is not None:
                self._apply_refreshed_snapshot(snapshot)
        if event.state in (
            WorkerState.SUCCESS,
            WorkerState.ERROR,
            WorkerState.CANCELLED,
        ):
            self._refresh_worker = None

    def _load_snapshot(self) -> DashboardSnapshot:
        return load_dashboard_snapshot(self.cwd)

    def _apply_refreshed_snapshot(self, snapshot: DashboardSnapshot) -> None:
        reload_data = _entries_signature(snapshot) != self._entries_signature
        self.snapshot = snapshot
        self._entries_signature = _entries_signature(snapshot)
        self._last_refreshed_at = datetime.now().astimezone()
        valid_keys = {
            *(entry.key for entry in self.snapshot.entries),
            *(entry.key for entry in self.snapshot.ongoing),
        }
        if self.selected_key not in valid_keys:
            self.selected_key = _snapshot_selected_key(self.snapshot)
            reload_data = True
        self._apply_snapshot("refreshed", reload_data=reload_data)

    def _apply_snapshot(self, status: str, *, reload_data: bool = True) -> None:
        self._syncing_selection = True
        try:
            self.query_one(DashboardHeader).set_snapshot(self.snapshot, self._last_refreshed_at)
            self.query_one(FrontierPane).set_snapshot(self.snapshot, self._selected_record_sha())
            if reload_data:
                self.query_one(ExperimentTreePane).set_snapshot(self.snapshot, self.selected_key)
                self.query_one(ExperimentsPane).set_snapshot(self.snapshot, self.selected_key)
                self.query_one(ExperimentTreePane).select_key(self.selected_key)
                self.query_one(ExperimentsPane).select_key(self.selected_key)
            else:
                self.query_one(ExperimentsPane).refresh_relative_fields(
                    self.snapshot, self.selected_key
                )
        finally:
            self._syncing_selection = False

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "experiments" or self._syncing_selection or self._resizing:
            return
        key = self.query_one(ExperimentsPane).selected_key
        if key is not None:
            self._set_selected_key(key, source="table")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "experiments" or self._resizing:
            return
        key = self.query_one(ExperimentsPane).selected_key
        if key is not None:
            self._set_selected_key(key, source="table")

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[str | None]) -> None:
        if (
            event.control.id != "tree"
            or self._syncing_selection
            or self._resizing
            or self.focused is not event.control
            or not self._widgets_ready()
            or not self._interaction_ready
        ):
            return
        if isinstance(event.node.data, str):
            self.query_one(ExperimentTreePane)._center_node_x(event.node)
            self._set_selected_key(event.node.data, source="tree")

    def on_tree_node_selected(self, event: Tree.NodeSelected[str | None]) -> None:
        if (
            event.control.id != "tree"
            or self._syncing_selection
            or self._resizing
            or self.focused is not event.control
            or not self._widgets_ready()
            or not self._interaction_ready
        ):
            return
        if isinstance(event.node.data, str):
            self._set_selected_key(event.node.data, source="tree")

    def _set_selected_key(self, key: str, *, source: str) -> None:
        if key == self.selected_key:
            return
        self.selected_key = key
        self._syncing_selection = True
        try:
            if source != "table":
                self.query_one(ExperimentsPane).select_key(key)
            if source != "tree":
                self.query_one(ExperimentTreePane).select_key(
                    key,
                    center=source == "table",
                )
            self.query_one(FrontierPane).set_snapshot(self.snapshot, self._selected_record_sha())
        finally:
            self._syncing_selection = False

    def _widgets_ready(self) -> bool:
        try:
            self.query_one(ExperimentsPane)
            self.query_one(ExperimentTreePane)
        except NoMatches:
            return False
        return True

    def _enable_interaction(self) -> None:
        self._interaction_ready = True

    def _finish_resize(self) -> None:
        self._resizing = False
        self._resize_timer = None
        if not self._widgets_ready():
            return
        table = self.query_one(ExperimentsPane)
        tree = self.query_one(ExperimentTreePane)
        if self.focused is table and table.selected_key is not None:
            self.selected_key = table.selected_key
        elif (
            self.focused is tree
            and tree.cursor_node is not None
            and isinstance(tree.cursor_node.data, str)
        ):
            self.selected_key = tree.cursor_node.data
        tree.select_key(self.selected_key)
        table.select_key(self.selected_key)

    def _selected_record_sha(self) -> str | None:
        entry = next(
            (item for item in self.snapshot.entries if item.key == self.selected_key), None
        )
        return entry.sha if entry is not None else None


def load_dashboard_snapshot(cwd: str | Path = ".") -> DashboardSnapshot:
    cwd_path = Path(cwd).resolve()
    try:
        repository = ExperimentRepository(cwd)
    except RuntimeError:
        return _empty_snapshot(
            cwd_path,
            message="Waiting for a git repository in this directory.",
        )

    ongoing_entries = _ongoing_entries(repository)
    records = sorted(repository.index(), key=lambda record: _parse_date(record.date))
    try:
        problem = repository.problem()
    except (FileNotFoundError, ValueError):
        return _empty_snapshot(
            repository.root,
            message="Waiting for a valid PROBLEM.md.",
            records_count=len(records),
            ongoing=ongoing_entries,
        )

    entries: list[DashboardEntry] = []
    frontier: list[FrontierPoint] = []
    best_record: ExperimentIndexEntry | None = None
    best_score: float | None = None
    improvement_count = 0
    for record in records:
        score = _numeric_metric(record, problem.metric)
        if score is None:
            continue
        delta = None if best_score is None else _score_delta(score, best_score, problem.direction)
        improved = delta is not None and delta > 0
        if improved:
            improvement_count += 1
        if best_score is None or _is_better(score, best_score, problem.direction):
            best_score = score
            best_record = record
        assert best_score is not None
        frontier.append(
            FrontierPoint(
                timestamp=_parse_date(record.date),
                score=score,
                frontier=best_score,
                improved=improved,
            )
        )
        previous = repository.previous_record(record)
        entries.append(
            DashboardEntry(
                key=record.sha,
                number=len(entries) + 1,
                sha=record.sha,
                ref=record.sha[:7],
                summary=record.document.summary,
                score=score,
                delta=delta,
                age=_relative_age(record.date),
                timestamp=_parse_date(record.date),
                parent_key=None if previous is None else previous.sha,
                improved=improved,
            )
        )

    if best_record is None or best_score is None or not frontier:
        message = (
            f'Waiting for recorded experiments with numeric "{problem.metric}" values.'
            if records
            else "Waiting for recorded experiments."
        )
        return _empty_snapshot(
            repository.root,
            message=message,
            metric=problem.metric,
            direction=problem.direction,
            records_count=len(records),
            ongoing=ongoing_entries,
        )

    latest = max(records, key=lambda record: _parse_date(record.date))
    ongoing_entries = [
        OngoingEntry(
            key=entry.key,
            number=len(entries) + len(ongoing_entries) - index,
            ref=entry.ref,
            summary=entry.summary,
            path=entry.path,
            branch=entry.branch,
            head=entry.head,
            parent_key=entry.parent_key,
            score=entry.score,
            delta=entry.delta,
            age=entry.age,
        )
        for index, entry in enumerate(ongoing_entries)
    ]

    return DashboardSnapshot(
        root_path=repository.root,
        metric=problem.metric,
        direction=problem.direction,
        status_message=None,
        records_count=len(records),
        ongoing_count=len(ongoing_entries),
        improvement_count=improvement_count,
        best_sha=best_record.sha,
        best_score=best_score,
        best_age=_relative_age(best_record.date),
        latest_sha=latest.sha,
        latest_summary=latest.document.summary,
        latest_age=_relative_age(latest.date),
        entries=tuple(entries),
        ongoing=tuple(ongoing_entries),
        frontier=tuple(frontier),
    )


def _snapshot_selected_key(snapshot: DashboardSnapshot) -> str:
    if snapshot.best_sha:
        return snapshot.best_sha
    if snapshot.ongoing:
        return snapshot.ongoing[0].key
    if snapshot.entries:
        return snapshot.entries[-1].key
    return ""


def _frontier_header(snapshot: DashboardSnapshot) -> Text:
    text = Text()
    text.append(" FRONTIER ", style="bold #f9fafb")
    text.append(f"{snapshot.records_count} experiments  ", style="#a8b0bc")
    if snapshot.ongoing_count:
        text.append(f"{snapshot.ongoing_count} ongoing  ", style="#a8b0bc")
    if snapshot.frontier:
        text.append(f"{snapshot.improvement_count} improvements  ", style="bold #5ee9b5")
        text.append(f"best {snapshot.best_score}  ", style="bold #f9fafb")
        text.append(snapshot.best_age, style="#a8b0bc")
    return text


def _frontier_chart(
    snapshot: DashboardSnapshot,
    *,
    width: int,
    height: int,
    selected_sha: str | None = None,
) -> list[Text]:
    label_width = 8
    plot_margin = 2
    plot_width = max(width - label_width - 1 - plot_margin * 2, 26)
    plot_height = max(height - 2, 4)
    points = snapshot.frontier
    min_score = min(point.frontier for point in points)
    max_score = max(point.frontier for point in points)
    if math.isclose(min_score, max_score):
        min_score -= 1
        max_score += 1
    score_range = max_score - min_score
    min_score -= score_range * 0.02
    max_score += score_range * 0.14
    dot_width = plot_width * 2
    dot_height = plot_height * 4

    grid_bits = [[0 for _ in range(plot_width)] for _ in range(plot_height)]
    frontier_bits = [[0 for _ in range(plot_width)] for _ in range(plot_height)]

    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = int(round((dot_height - 1) * fraction))
        for x in range(dot_width):
            _set_braille_dot(grid_bits, x, y)

    frontier_points = [
        _chart_point(
            index,
            len(points),
            point.frontier,
            min_score,
            max_score,
            dot_width,
            dot_height,
        )
        for index, point in enumerate(points)
    ]
    for index, (x, y) in enumerate(frontier_points):
        if index:
            previous_x, previous_y = frontier_points[index - 1]
            x_start, x_end = sorted((previous_x, x))
            for step_x in range(x_start, x_end + 1):
                _set_braille_dot(frontier_bits, step_x, previous_y)
            y_start, y_end = sorted((previous_y, y))
            for step_y in range(y_start, y_end + 1):
                _set_braille_dot(frontier_bits, x, step_y)
        _set_braille_dot(frontier_bits, x, y)

    marker_columns = {
        _plot_column(index, len(points), plot_width)
        for index, point in enumerate(points)
        if point.improved
    }
    selected_column = None
    if selected_sha is not None:
        for index, entry in enumerate(snapshot.entries):
            if entry.sha == selected_sha:
                selected_column = _plot_column(index, len(points), plot_width)
                break

    tick_rows = {
        0: _format_axis_score(max_score),
        plot_height // 2: _format_axis_score((min_score + max_score) / 2),
        plot_height - 1: _format_axis_score(min_score),
    }
    lines: list[Text] = []
    for y in range(plot_height):
        label = tick_rows.get(y, "")
        line = Text(f"{label:>{label_width}} " + (" " * plot_margin))
        for x in range(plot_width):
            frontier_cell = frontier_bits[y][x]
            grid_cell = grid_bits[y][x]
            if selected_column is not None and x == selected_column:
                line.append("│", style="#7c8798")
            elif frontier_cell:
                line.append(chr(0x2800 + frontier_cell), style="#5ee9b5")
            elif grid_cell:
                line.append(chr(0x2800 + grid_cell), style="#2a3038")
            else:
                line.append(" ")
        line.append(" " * plot_margin)
        lines.append(line)

    axis_line = Text(" " * (label_width + 1 + plot_margin))
    for x in range(plot_width):
        axis_line.append(
            "▴" if x in marker_columns else "─",
            style="#5ee9b5" if x in marker_columns else "#2a3038",
        )
    axis_line.append(" " * plot_margin)
    lines.append(axis_line)

    middle_count = max(1, (len(points) + 1) // 2)
    count_labels = ((1, "1"), (middle_count, str(middle_count)), (len(points), str(len(points))))
    axis = [" "] * (plot_width + label_width + 1 + plot_margin * 2)
    for count, label in count_labels:
        if len(points) == 1:
            position = label_width + 1 + plot_margin
        else:
            fraction = (count - 1) / (len(points) - 1)
            position = (
                label_width + 1 + plot_margin + int(round(fraction * (plot_width - len(label))))
            )
        position = max(0, min(position, len(axis) - len(label)))
        axis[position : position + len(label)] = list(label)
    lines.append(Text("".join(axis), style="#a8b0bc"))
    return lines


def _score_to_y(score: float, minimum: float, maximum: float, height: int) -> int:
    fraction = (score - minimum) / (maximum - minimum)
    return height - 1 - int(round(fraction * (height - 1)))


def _chart_point(
    index: int,
    total: int,
    score: float,
    min_score: float,
    max_score: float,
    dot_width: int,
    dot_height: int,
) -> tuple[int, int]:
    x = _plot_column(index, total, dot_width)
    y = _score_to_y(score, min_score, max_score, dot_height)
    return (x, y)


def _plot_column(index: int, total: int, width: int) -> int:
    fraction = 0.0 if total <= 1 else index / (total - 1)
    return int(round(fraction * (width - 1)))


def _set_braille_dot(cells: list[list[int]], x: int, y: int) -> None:
    if x < 0 or y < 0:
        return
    cell_x = x // 2
    cell_y = y // 4
    if cell_y >= len(cells) or cell_x >= len(cells[cell_y]):
        return
    bit = _BRAILLE_BITS[(x % 2, y % 4)]
    cells[cell_y][cell_x] |= bit


def _title(value: str) -> Text:
    return Text(value.upper(), style="bold #f9fafb")


def _subtle(value: str) -> Text:
    return Text(value, style="#a8b0bc")


def _tree_label(entry: DashboardRow) -> Text:
    text = Text()
    if isinstance(entry, DashboardEntry):
        text.append("● ", style="#5ee9b5" if entry.improved else "#6b7280")
        text.append(f"#{entry.number}: {entry.ref}", style="bold #f3f4f6")
        text.append(f"  {_format_score(entry.score)}", style="#8b95a7")
    else:
        text.append("◌ ", style="#7c8798")
        text.append(entry.ref, style="bold #9fb0c4")
    return text


def _detail_title(entry: DashboardEntry) -> Text:
    text = Text()
    text.append(f"#{entry.number}", style="bold #5ee9b5")
    text.append("  ", style="#f3f4f6")
    text.append(entry.sha, style="bold #f3f4f6")
    text.append(f"  {_format_score(entry.score)}", style="#d6dbe3")
    text.append(f"  {entry.timestamp.isoformat(timespec='seconds')}", style="#8b95a7")
    return text


def _combined_lineage_text(
    repository: ExperimentRepository,
    record: ExperimentIndexEntry,
) -> tuple[Text, int]:
    upstream_graph = repository.lineage(
        record.sha,
        edges=GraphEdges.ALL,
        direction=GraphDirection.BACKWARD,
        depth=None,
    )
    downstream_graph = repository.lineage(
        record.sha,
        edges=GraphEdges.ALL,
        direction=GraphDirection.FORWARD,
        depth=None,
    )
    numbers = {
        entry.sha: index
        for index, entry in enumerate(
            sorted(repository.index(), key=lambda item: _parse_date(item.date)),
            start=1,
        )
    }
    present = set(upstream_graph.node_order) | set(downstream_graph.node_order)
    ordered = [
        entry
        for entry in sorted(repository.index(), key=lambda item: _parse_date(item.date))
        if entry.sha in present
    ]

    incoming: dict[str, list[LineageEdge]] = {}
    for edge in (*upstream_graph.edges, *downstream_graph.edges):
        if edge.source in present and edge.target in present:
            incoming.setdefault(edge.source, []).append(edge)

    parent_by_child: dict[str, str | None] = {}
    extras_by_child: dict[str, tuple[str, ...]] = {}
    for entry in ordered:
        edges = incoming.get(entry.sha, [])
        primary = _primary_lineage_parent(entry.sha, edges, numbers)
        parent_by_child[entry.sha] = primary.target if primary is not None else None
        extras_by_child[entry.sha] = tuple(
            label
            for edge in edges
            if edge is not primary
            for label in [_lineage_edge_label(edge, numbers)]
            if label is not None
        )

    children_by_parent: dict[str | None, list[str]] = {}
    for entry in ordered:
        parent = parent_by_child.get(entry.sha)
        if parent is None or parent not in present or parent == entry.sha:
            children_by_parent.setdefault(None, []).append(entry.sha)
        else:
            children_by_parent.setdefault(parent, []).append(entry.sha)

    for children in children_by_parent.values():
        children.sort(key=lambda sha: numbers.get(sha, 10**9))

    lines: list[Text] = []
    current_line = 0
    direct_parents = sum(1 for edge in upstream_graph.edges if edge.source == record.sha)
    direct_children = sum(1 for edge in downstream_graph.edges if edge.target == record.sha)
    roots = children_by_parent.get(None, [])
    leaves = sum(1 for entry in ordered if not children_by_parent.get(entry.sha))

    lines.append(
        _lineage_stats_line(
            ("ancestors", len(upstream_graph.node_order) - 1),
            ("descendants", len(downstream_graph.node_order) - 1),
            ("parents", direct_parents),
            ("children", direct_children),
        )
    )
    lines.append(Text())
    lines.append(
        _lineage_stats_line(
            ("roots", len(roots)),
            ("leaves", leaves),
            ("nodes", len(ordered)),
        )
    )
    lines.append(Text())

    def render_node(
        sha: str, prefix: str = "", *, is_last: bool = True, root: bool = False
    ) -> None:
        nonlocal current_line
        child = repository.record_by_sha(sha)
        if child is None:
            return
        line = Text()
        if not root:
            line.append(prefix, style="#4b5563")
            line.append("└─ " if is_last else "├─ ", style="#4b5563")
        line.append_text(
            _lineage_node_text(
                child,
                number=numbers.get(child.sha),
                highlight=child.sha == record.sha,
            )
        )
        extras = extras_by_child.get(child.sha, ())
        if extras:
            line.append("  ", style="#f3f4f6")
            line.append(f"[{', '.join(extras)}]", style="#8b95a7")
        if child.sha == record.sha:
            current_line = len(lines)
        lines.append(line)
        branch_prefix = prefix + ("   " if is_last else "│  ")
        children = children_by_parent.get(child.sha, [])
        for index, child_sha in enumerate(children):
            render_node(child_sha, branch_prefix, is_last=index == len(children) - 1)

    roots = children_by_parent.get(None, [])
    for index, root_sha in enumerate(roots):
        render_node(root_sha, is_last=index == len(roots) - 1, root=True)

    text = Text()
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
        text.append_text(line)
    text.no_wrap = True
    text.overflow = "ignore"
    return text, current_line


def _primary_lineage_parent(
    child_sha: str,
    edges: list[LineageEdge],
    numbers: dict[str, int],
) -> LineageEdge | None:
    child_number = numbers.get(child_sha, 10**9)

    def sort_key(edge: LineageEdge) -> tuple[int, int, int, str]:
        target_number = numbers.get(edge.target, 10**9)
        newer_or_equal = 1 if target_number >= child_number else 0
        non_git = 1 if edge.kind != "git" else 0
        return (newer_or_equal, non_git, target_number, edge.target)

    return min(edges, key=sort_key, default=None)


def _lineage_node_text(
    record: ExperimentIndexEntry,
    *,
    number: int | None,
    highlight: bool = False,
) -> Text:
    text = Text()
    if number is not None:
        text.append(f"#{number}", style="bold #5ee9b5" if highlight else "#8b95a7")
        text.append("  ", style="#f3f4f6")
    text.append(record.sha[:7], style="bold #e5e7eb" if highlight else "#d6dbe3")
    text.append("  ", style="#f3f4f6")
    text.append(record.document.summary, style="#d6dbe3" if highlight else "#cbd5e1")
    return text


def _lineage_edge_label(edge: LineageEdge, numbers: dict[str, int]) -> str | None:
    number = numbers.get(edge.target)
    label = f"#{number}" if number is not None else edge.target[:7]
    if edge.kind == "git":
        return None
    return f"ref {label}"


def _lineage_stats_line(*pairs: tuple[str, int]) -> Text:
    text = Text()
    for index, (label, value) in enumerate(pairs):
        if index:
            text.append("  ", style="#0d0f13")
        text.append(" ", style="on #161b22")
        text.append(label.upper(), style="bold #8b95a7 on #161b22")
        text.append(" ", style="on #161b22")
        text.append(str(value), style="bold #f3f4f6 on #161b22")
        text.append(" ", style="on #161b22")
    return text


def _experiment_summary_text(record: ExperimentIndexEntry) -> Text:
    text = Text()
    text.append("SUMMARY\n", style="bold #9aa4b2")
    text.append(record.document.summary, style="#f3f4f6")
    text.append("\n\n", style="#f3f4f6")
    text.append("METRICS\n", style="bold #9aa4b2")
    if record.document.metrics:
        for name, value in record.document.metrics.items():
            text.append(f"{name:<16}", style="#8b95a7")
            text.append(json.dumps(value), style="#f3f4f6")
            text.append("\n")
    else:
        text.append("(none)\n", style="#8b95a7")
    text.append("\n", style="#f3f4f6")
    text.append("REFERENCES\n", style="bold #9aa4b2")
    if record.document.references:
        for index, reference in enumerate(record.document.references):
            text.append(reference.commit[:7], style="bold #f3f4f6")
            text.append("  ", style="#f3f4f6")
            text.append(reference.why, style="#cbd5e1")
            text.append("\n")
            if index != len(record.document.references) - 1:
                text.append("\n")
    else:
        text.append("(none)", style="#8b95a7")
    return text


def _code_changes_view(
    base: str | None, has_previous: bool, patch: GitDiff | None
) -> CodeChangesView:
    text = Text()
    text.append("BASE\n", style="bold #9aa4b2")
    if base is not None:
        relation = "previous experiment" if has_previous else "first parent"
        text.append(base[:7], style="bold #f3f4f6")
        text.append(f" ({relation})", style="#8b95a7")
    else:
        text.append("(none)", style="#8b95a7")
    text.append("\n\n", style="#f3f4f6")
    text.append("SUMMARY\n", style="bold #9aa4b2")

    if patch is None:
        text.append("No code changes.", style="#8b95a7")
        return CodeChangesView(summary=text, files=())

    chunks = _diff_chunks_by_path(patch.patch)
    files: list[CodeChangeFile] = []
    for changed in patch.changed_paths:
        raw = chunks.get(changed.path)
        if raw is None and changed.previous_path is not None:
            raw = chunks.get(changed.previous_path)
        if raw is None:
            raw = _fallback_diff(changed)
        additions, deletions = _diff_line_counts(raw)
        files.append(
            CodeChangeFile(
                path=changed.path,
                status=changed.status[0],
                display_path=(
                    f"{changed.previous_path} -> {changed.path}"
                    if changed.previous_path is not None and changed.previous_path != changed.path
                    else changed.path
                ),
                additions=additions,
                deletions=deletions,
                diff=_styled_diff_text(raw),
            )
        )
    additions = sum(item.additions for item in files)
    deletions = sum(item.deletions for item in files)
    text.append(
        f"{len(files)} file{'s' if len(files) != 1 else ''} changed",
        style="#f3f4f6",
    )
    if additions or deletions:
        text.append(", ", style="#f3f4f6")
        text.append(f"+{additions} insertions", style=_POSITIVE_COLOR)
        text.append(", ", style="#f3f4f6")
        text.append(f"-{deletions} deletions", style=_NEGATIVE_COLOR)
    text.append("\n\n", style="#f3f4f6")
    text.append("FILES\n", style="bold #9aa4b2")
    text.append(str(len(files)), style="bold #f3f4f6")
    return CodeChangesView(summary=text, files=tuple(files))


def _diff_chunks_by_path(patch: str) -> dict[str, str]:
    chunks: dict[str, str] = {}
    current_path: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_path, current_lines
        if current_path is not None and current_lines:
            chunks[current_path] = "\n".join(current_lines)
        current_path = None
        current_lines = []

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            flush()
            current_path = _path_from_diff_header(line)
            current_lines = [line]
            continue
        if current_path is not None:
            current_lines.append(line)
    flush()
    return chunks


def _path_from_diff_header(line: str) -> str | None:
    parts = line.split()
    if len(parts) < 4:
        return None
    path = parts[3]
    return path[2:] if path.startswith("b/") else path


def _fallback_diff(changed: GitChangedPath) -> str:
    previous = changed.previous_path or changed.path
    return (
        f"diff --git a/{previous} b/{changed.path}\n"
        f"--- a/{previous}\n"
        f"+++ b/{changed.path}\n"
        "(patch unavailable)\n"
    )


def _diff_line_counts(patch: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return additions, deletions


def _styled_diff_text(patch: str) -> Text:
    text = Text()
    for line in patch.splitlines():
        if line.startswith(("diff --git", "index ", "--- ", "+++ ")):
            continue
        start = len(text.plain)
        text.append(line)
        end = len(text.plain)
        if line.startswith("@@"):
            text.stylize("#b8c1cc", start, end)
        elif line.startswith("+") and not line.startswith("+++"):
            text.stylize(_POSITIVE_COLOR, start, end)
        elif line.startswith("-") and not line.startswith("---"):
            text.stylize(_NEGATIVE_COLOR, start, end)
        else:
            text.stylize("#f3f4f6", start, end)
        text.append("\n")
    if not patch:
        text.append("(none)", style="#8b95a7")
    return text


def _node_depth(node: TreeNode[str | None]) -> int:
    depth = 0
    current = node.parent
    while current is not None and current.parent is not None:
        depth += 1
        current = current.parent
    return depth


def _entries_signature(snapshot: DashboardSnapshot) -> tuple[tuple[object, ...], ...]:
    return (
        ((snapshot.status_message,),)
        + tuple(
            (
                entry.key,
                entry.parent_key,
                entry.summary,
                entry.score,
                entry.delta,
                entry.improved,
            )
            for entry in snapshot.entries
        )
        + tuple(
            (
                ongoing.key,
                ongoing.parent_key,
                ongoing.ref,
                ongoing.summary,
            )
            for ongoing in snapshot.ongoing
        )
    )


def _is_recorded(entry: DashboardRow) -> bool:
    return isinstance(entry, DashboardEntry)


def _ordered_dashboard_rows(snapshot: DashboardSnapshot) -> list[DashboardRow]:
    return [
        *sorted(snapshot.ongoing, key=lambda entry: entry.ref.lower()),
        *sorted(snapshot.entries, key=lambda entry: entry.number, reverse=True),
    ]


def _tree_sort_key(entry: DashboardRow) -> tuple[int, int | str]:
    if isinstance(entry, DashboardEntry):
        return (0, entry.number)
    return (1, entry.ref.lower())


def _table_number_cell(entry: DashboardRow) -> Text:
    if isinstance(entry, DashboardEntry):
        return Text(str(entry.number), style="#f3f4f6")
    return Text(str(entry.number), style="#d6dbe3")


def _table_ref_cell(entry: DashboardRow, width: int) -> Text:
    if isinstance(entry, DashboardEntry):
        return Text(entry.ref, style="#f3f4f6")
    return Text("--", style="#8b95a7")


def _table_summary_cell(entry: DashboardRow, width: int) -> Text:
    if isinstance(entry, DashboardEntry):
        return Text(_truncate(entry.summary, width), style="#f3f4f6")
    text = Text()
    prefix = f"{entry.ref}: "
    text.append(prefix, style="bold #d6dbe3")
    text.append(_truncate(entry.summary, max(width - len(prefix), 0)), style="#cbd5e1")
    return text


def _table_score_cell(entry: DashboardRow) -> Text:
    if isinstance(entry, DashboardEntry):
        return Text(_format_score(entry.score), style="bold #f9fafb")
    return Text(_ongoing_score_placeholder(), style="#8b95a7")


def _table_delta_cell(entry: DashboardRow) -> Text:
    if isinstance(entry, DashboardEntry):
        return Text(_format_delta(entry.delta), style=_delta_color(entry.delta))
    return Text(_ongoing_placeholder(), style="#8b95a7")


def _table_age(entry: DashboardRow) -> str:
    return entry.age or _ongoing_placeholder()


def _ongoing_score_placeholder() -> str:
    return "pending"


def _ongoing_placeholder() -> str:
    return "--"


def _ongoing_entries(repository: ExperimentRepository) -> list[OngoingEntry]:
    entries: list[OngoingEntry] = []
    for worktree in repository.active_worktrees():
        if not worktree.is_managed or worktree.is_primary or worktree.is_missing:
            continue
        nearest = repository.nearest_record(worktree.head)
        entries.append(
            OngoingEntry(
                key=_ongoing_key(worktree.path),
                number=0,
                ref=_ongoing_ref(worktree),
                summary=_ongoing_summary(worktree.path),
                path=worktree.path,
                branch=worktree.branch,
                head=worktree.head,
                parent_key=None if nearest is None else nearest.sha,
                age="",
            )
        )
    entries.sort(key=lambda entry: entry.ref.lower())
    return entries


def _ongoing_key(path: Path) -> str:
    return f"worktree:{path.resolve()}"


def _ongoing_ref(worktree: object) -> str:
    branch = getattr(worktree, "branch", None)
    name = getattr(worktree, "name", None)
    if isinstance(branch, str) and branch:
        return branch.removeprefix("autoevolve/")
    if isinstance(name, str) and name:
        return name
    path = getattr(worktree, "path", None)
    return Path(path).name if path is not None else "ongoing"


def _ongoing_summary(path: Path) -> str:
    experiment_path = path / EXPERIMENT_FILE
    if not experiment_path.exists():
        return "(missing EXPERIMENT.json)"
    try:
        return parse_experiment_document(experiment_path.read_text(encoding="utf-8")).summary
    except ValueError:
        return "(invalid EXPERIMENT.json)"


def _numeric_metric(record: ExperimentIndexEntry, metric: str) -> float | None:
    value = record.document.metrics.get(metric)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _is_better(value: float, best: float, direction: MetricDirection) -> bool:
    return value > best if direction == "max" else value < best


def _score_delta(current: float, previous: float, direction: MetricDirection) -> float:
    return current - previous if direction == "max" else previous - current


def _row_sort_key(entry: DashboardEntry, direction: MetricDirection) -> tuple[float, str]:
    ranked = -entry.score if direction == "max" else entry.score
    return (ranked, entry.sha)


def _format_score(value: float) -> str:
    return f"{value:.8g}"


def _format_axis_score(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _format_delta(value: float | None) -> str:
    if value is None:
        return "--"
    if value == 0:
        return "+0"
    return f"{value:+.3e}"


def _delta_color(value: float | None) -> str:
    if value is None or value == 0:
        return "#8b95a7"
    return _POSITIVE_COLOR if value > 0 else _NEGATIVE_COLOR


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return value[: max(width - 3, 0)].rstrip() + "..."


def _parse_date(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _relative_age(value: str) -> str:
    delta = datetime.now(timezone.utc) - _parse_date(value).astimezone(timezone.utc)
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"
