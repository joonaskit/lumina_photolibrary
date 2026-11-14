"""A row of controls that wraps: widgets go left to right and start another row when the
next one doesn't fit, so a long row doesn't set the window's minimum width (#143)."""

from PySide6.QtCore import QRect, QSize
from PySide6.QtWidgets import QLayout

# How a widget sits in its row
LEADING = "leading"
# At the right end of its row, as after a stretch
TRAILING = "trailing"
# A thin divider, left out where a row starts or ends
SEPARATOR = "separator"


class FlowLayout(QLayout):
    """Widgets in rows, each as wide as its size hint, centred on the row's height. The
    minimum width is the widest widget's; the height follows the width."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []
        self._kinds = []
        self._kind = LEADING

    def add_trailing(self, widget):
        """Add `widget` at the right end of its row."""
        self._add(widget, TRAILING)

    def add_separator(self, widget):
        """Add a divider between the widgets either side of it, hidden at a row's end."""
        self._add(widget, SEPARATOR)

    def _add(self, widget, kind):
        self._kind = kind
        try:
            self.addWidget(widget)
        finally:
            self._kind = LEADING

    # QLayout

    def addItem(self, item):
        self._items.append(item)
        self._kinds.append(self._kind)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        if not 0 <= index < len(self._items):
            return None
        del self._kinds[index]
        return self._items.pop(index)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._place(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._place(rect, apply=True)

    def sizeHint(self):
        """Everything on one row."""
        shown = self._shown()
        width = sum(item.sizeHint().width() for item, _kind in shown)
        width += self._spacing() * max(len(shown) - 1, 0)
        height = max((item.sizeHint().height() for item, _kind in shown), default=0)
        return self._margins_around(QSize(width, height))

    def minimumSize(self):
        """As wide as the widest widget, one row tall: the height for a narrower width
        comes from heightForWidth."""
        shown = self._shown()
        width = max((item.minimumSize().width() for item, _kind in shown), default=0)
        height = max((item.sizeHint().height() for item, _kind in shown), default=0)
        return self._margins_around(QSize(width, height))

    # Laid out

    def _spacing(self):
        return max(self.spacing(), 0)

    def _shown(self):
        pairs = zip(self._items, self._kinds, strict=True)
        return [(item, kind) for item, kind in pairs if not item.isEmpty()]

    def _margins_around(self, size):
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _rows(self, width):
        """The shown items in rows that fit `width`, each with the width it gets."""
        space = self._spacing()
        rows, row, used = [], [], 0
        for item, kind in self._shown():
            hint = item.sizeHint().width()
            item_width = max(min(hint, width), item.minimumSize().width())
            if row and used + space + item_width > width:
                rows.append(row)
                row, used = [], 0
            used += (space if row else 0) + item_width
            row.append((item, kind, item_width))
        if row:
            rows.append(row)
        # A divider at a row's start or end divides nothing
        return [
            [
                entry
                for index, entry in enumerate(row)
                if entry[1] != SEPARATOR or 0 < index < len(row) - 1
            ]
            for row in rows
        ]

    def _place(self, rect, apply):
        """Lay the items out in `rect` when `apply`; the height they take either way."""
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        space = self._spacing()
        rows = self._rows(area.width())
        if apply:
            placed = {id(item) for row in rows for item, _kind, _width in row}
            for item, _kind in self._shown():
                if id(item) not in placed and item.widget() is not None:
                    # Out of sight: the item leaves a widget where it was for a rect
                    # smaller than the widget
                    item.widget().move(-10000, -10000)
        y = area.y()
        for row in rows:
            height = max(item.sizeHint().height() for item, _kind, _width in row)
            if apply:
                left = area.x()
                for item, kind, width in row:
                    if kind != TRAILING:
                        self._put(item, left, y, width, height)
                        left += width + space
                right = area.x() + area.width()
                for item, kind, width in reversed(row):
                    if kind == TRAILING:
                        right -= width
                        self._put(item, right, y, width, height)
                        right -= space
            y += height + space
        used = y - area.y() - (space if rows else 0)
        return used + margins.top() + margins.bottom()

    @staticmethod
    def _put(item, x, y, width, row_height):
        height = min(item.sizeHint().height(), row_height)
        item.setGeometry(QRect(x, y + (row_height - height) // 2, width, height))
