"""The grid's page: the size of its thumbnails, how it shows thumbs-down photos, its place
kept while a scan adds photos, and the thumbnails it waits for."""

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import QListView

from lumina.integration import style
from lumina.ui import settings
from lumina.ui.grid_size import NAMES as GRID_SIZE_NAMES
from lumina.ui.window_common import (
    _TRIM_MS,
    GRID_CELL_EXTRA,
    GRID_GAP,
    GRID_ICONS,
    _inset,
    _margins,
)


class GridPage(QObject):
    # A click on the stack badge of the cell of `row`
    stack_badge_clicked = Signal(int)

    def __init__(self, grid, delegate, size_switch, model, parent=None):
        """`grid` is the grid's list view, `delegate` its cells' delegate and `size_switch`
        the GridSizeSwitch in the status bar."""
        super().__init__(parent)
        self.grid = grid
        self.delegate = delegate
        self.size_switch = size_switch
        self.model = model
        # The photo at the top of the view, the current one, and how far the top one's
        # cell is from the view's top, while the rows are refreshed
        self.place = (None, None, 0)
        size_switch.changed.connect(self.set_grid_size)
        self._apply_grid_size(settings.grid_size())
        # Closer cells in a compact window; the thumbnails stay as large
        style.follow_density(grid, lambda: self._apply_grid_size(size_switch.value()), now=False)
        grid.viewport().installEventFilter(self)
        model.about_to_refresh.connect(self.save_place)
        model.refreshed.connect(self.restore_place)
        # Scrolling fast asks for a thumbnail of every row painted on the way: once it
        # settles, those far from the view are not waited for
        self._trim_timer = QTimer(self)
        self._trim_timer.setSingleShot(True)
        self._trim_timer.setInterval(_TRIM_MS)
        self._trim_timer.timeout.connect(self.trim_thumbnails)
        grid.verticalScrollBar().valueChanged.connect(lambda _value: self._trim_timer.start())

    # The thumbnails' size

    def set_grid_size(self, name):
        """Show thumbnails small, medium or large, and remember it."""
        if name not in GRID_ICONS:
            return
        settings.set_grid_size(name)
        self._apply_grid_size(name)

    def step_grid_size(self, delta):
        """One size larger (1) or smaller (-1), if there is one."""
        index = GRID_SIZE_NAMES.index(self.size_switch.value()) + delta
        if 0 <= index < len(GRID_SIZE_NAMES):
            self.set_grid_size(GRID_SIZE_NAMES[index])

    def _apply_grid_size(self, name):
        self.size_switch.set_value(name)
        side = style.px(GRID_ICONS[name])
        cell = QSize(
            side + style.gap(GRID_CELL_EXTRA.width()), side + style.gap(GRID_CELL_EXTRA.height())
        )
        self.grid.setIconSize(QSize(side, side))
        self.delegate.set_size(cell.shrunkBy(_margins(style.gap(GRID_GAP))))
        self.grid.setGridSize(cell)
        # The photo the user was on stays in view
        if self.grid.currentIndex().isValid():
            self.grid.scrollTo(self.grid.currentIndex(), QListView.ScrollHint.PositionAtCenter)

    def set_rejected(self, mode):
        """Show, dim or hide thumbs-down photos, and remember it."""
        settings.set_rejected_photos(mode)
        self.model.set_rejected(mode)
        self.grid.viewport().update()

    # The thumbnails waited for

    def visible_rows(self):
        """(first, last) of the rows in the grid's view and a screenful above and below,
        or None while there are none."""
        cell = self.grid.gridSize()
        if self.model.rowCount() == 0 or cell.width() <= 0 or cell.height() <= 0:
            return None
        view = self.grid.viewport().size()
        columns = max(1, view.width() // cell.width())
        screenful = columns * (view.height() // cell.height() + 1)
        first = 0
        for x in range(cell.width() // 2, max(view.width(), 1), cell.width()):
            index = self.grid.indexAt(QPoint(x, 2))
            if index.isValid():
                first = index.row()
                break
        return max(0, first - screenful), first + 2 * screenful

    def trim_thumbnails(self):
        """Stop waiting for the thumbnails of rows far from the view."""
        rows = self.visible_rows()
        if rows is not None:
            self.model.keep_thumbnails(*rows)

    def eventFilter(self, watched, event):
        # Ctrl and the wheel over the grid: larger or smaller thumbnails
        if (
            event.type() == QEvent.Type.Wheel
            and watched is self.grid.viewport()
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            steps = event.angleDelta().y()
            if steps:
                self.step_grid_size(1 if steps > 0 else -1)
            return True
        # A click on a stack's or the virtual copies' badge opens or closes it, and chooses
        # nothing
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and watched is self.grid.viewport()
            and event.button() == Qt.MouseButton.LeftButton
        ):
            index = self.grid.indexAt(event.position().toPoint())
            if index.isValid() and self.delegate.stack_badge_at(self.grid, index, event.position()):
                self.stack_badge_clicked.emit(index.row())
                return True
            if index.isValid() and self.delegate.copies_badge_at(
                self.grid, index, event.position()
            ):
                self.model.toggle_copies(self.model.photo_id(index.row()))
                return True
        return super().eventFilter(watched, event)

    # Keeping the grid's place while a scan adds photos

    def save_place(self):
        top = self.grid.indexAt(self.grid.viewport().rect().topLeft() + _inset())
        current = self.grid.currentIndex()
        # How far the top photo's cell is above (negative) or below the view's top: a view
        # scrolled to the middle of a row stays so
        offset = self.grid.visualRect(top).top() - self.grid.viewport().rect().top()
        self.place = (
            self.model.photo_id(top.row()) if top.isValid() else None,
            self.model.photo_id(current.row()) if current.isValid() else None,
            offset if top.isValid() else 0,
        )

    def restore_place(self):
        top, current, offset = self.place
        for photo_id, make_current in ((current, True), (top, False)):
            row = self.model.row_of(photo_id) if photo_id is not None else None
            if row is None:
                continue
            index = self.model.index(row)
            if make_current:
                self.grid.setCurrentIndex(index)
            else:
                # The view lays its cells out later; where the photo is now is asked of it
                # when it is laid out
                self.grid.doItemsLayout()
                self.grid.scrollTo(index, QListView.ScrollHint.PositionAtTop)
                bar = self.grid.verticalScrollBar()
                bar.setValue(bar.value() - offset)
