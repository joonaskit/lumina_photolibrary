"""What can be done, found by looking: an overlay of the keyboard shortcuts (? or F1) and
a palette to find any action by name and run it (Ctrl+K). Both read the QActions of the
window and of what it shows, so they never go stale: an action is put in a group with
`set_group`, and says why it can't be used now with `enable`."""

from dataclasses import dataclass, field

import shiboken6
from PySide6.QtCore import QEvent, QRect, QSize, Qt
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPainter, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from lumina.integration import style

# The overlay's groups, in its order; an action in none is in the last
GROUPS = (
    "General",
    "Library",
    "Culling",
    "Viewer",
    "Develop",
    "Stacks and Copies",
    "Merges",
    "Camera",
    "Other",
)

# The palette's rows shown at once before it scrolls
_ROWS = 10


def set_group(name, *actions):
    """Put `actions` in the group `name` (one of GROUPS) of the overlay and the palette."""
    for action in actions:
        action.setProperty("group", name)


def enable(action, on, why=""):
    """Enable `action` or not; `why` is what the palette says of it while it is not."""
    action.setEnabled(on)
    action.setProperty("why", why)


def keys_of(action):
    """The action's shortcuts as the keyboard shows them, each once."""
    keys = []
    for sequence in action.shortcuts():
        text = sequence.toString(QKeySequence.SequenceFormat.NativeText)
        if text and text not in keys:
            keys.append(text)
    return keys


@dataclass
class Entry:
    """One thing the overlay or the palette shows: an action, or a key that is held
    rather than pressed (`action` None), from a widget's `shortcut_hints`."""

    name: str
    keys: list
    group: str
    action: QAction = None
    why: str = field(default="")

    @property
    def enabled(self):
        return self.action is None or self.action.isEnabled()


def _entry(action):
    group = action.property("group")
    return Entry(
        name=action.text().replace("&", ""),
        keys=keys_of(action),
        group=group if group in GROUPS else "Other",
        action=action,
        why=action.property("why") or "",
    )


def entries(window):
    """The actions of `window` and of every widget it shows, once each, in the order
    they were added; and the keys widgets shown take without an action. Actions with no
    text (a field's icon) are left out. Two actions of one name are one entry: the one
    with a shortcut."""
    found, by_name = [], {}
    shown = [window, *(w for w in window.findChildren(QWidget) if w.isVisible())]
    for widget in shown:
        for action in widget.actions():
            if action.isSeparator() or not action.text():
                continue
            entry = _entry(action)
            known = by_name.get(entry.name)
            if known is None:
                by_name[entry.name] = len(found)
                found.append(entry)
            elif not found[known].keys and entry.keys:
                found[known] = entry
        for group, name, keys in getattr(widget, "shortcut_hints", ()):
            if name not in by_name:
                by_name[name] = len(found)
                found.append(Entry(name=name, keys=list(keys), group=group))
    return found


def matching(query, found):
    """The entries whose name has every word of `query`, those that start with its first
    word first, then those with a word that does; otherwise in their order."""
    words = query.lower().split()
    if not words:
        return list(found)
    ranked = []
    for place, entry in enumerate(found):
        name = entry.name.lower()
        if not all(word in name for word in words):
            continue
        if name.startswith(words[0]):
            rank = 0
        elif any(part.startswith(words[0]) for part in name.split()):
            rank = 1
        else:
            rank = 2
        ranked.append((rank, place, entry))
    return [entry for _rank, _place, entry in sorted(ranked, key=lambda r: r[:2])]


class _Layer(QWidget):
    """Over the whole window, under a card: a click beside the card closes it, and keys
    are its own, not the window's shortcuts."""

    def __init__(self, window, dim):
        super().__init__(window)
        self._dim = dim
        self._focus = None
        self.hide()
        window.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self.parent() and event.type() == QEvent.Type.Resize and self.isVisible():
            self.setGeometry(self.parent().rect())
            self.place()
        return False

    def place(self):
        """Size and place the card in the window."""

    def open_layer(self):
        self._focus = QApplication.focusWidget()
        self.setGeometry(self.parent().rect())
        self.place()
        self.show()
        self.raise_()

    def close_layer(self):
        """Hide, giving the focus back to where it was."""
        self.hide()
        if self._focus is not None and shiboken6.isValid(self._focus):
            self._focus.setFocus()
        self._focus = None

    def paintEvent(self, _event):
        colour = self.palette().color(QPalette.ColorRole.Window)
        colour.setAlphaF(self._dim)
        QPainter(self).fillRect(self.rect(), colour)

    def mousePressEvent(self, _event):
        self.close_layer()


class ShortcutOverlay(_Layer):
    """The keyboard shortcuts of the view that is open, in groups. Any key or a click
    closes it."""

    def __init__(self, window):
        super().__init__(window, dim=0.85)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.card = QFrame(self)
        self.card.setObjectName("card")
        layout = QVBoxLayout(self.card)
        heading = QHBoxLayout()
        title = QLabel("Keyboard Shortcuts")
        title.setObjectName("title")
        self.view_label = QLabel()
        self.view_label.setObjectName("secondary")
        heading.addWidget(title)
        heading.addStretch(1)
        heading.addWidget(self.view_label)
        layout.addLayout(heading)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("shortcutsScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.scroll, 1)
        hint = QLabel("Any key closes this")
        hint.setObjectName("secondary")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)
        # The rows shown: (entry, its name's label)
        self.rows = []

        def space():
            margin = style.gap(20)
            layout.setContentsMargins(margin, margin, margin, margin)
            layout.setSpacing(style.gap(14))

        style.follow_density(self.card, space)

    def open(self, found, view=""):
        """Show the shortcuts among `found` (from `entries`): those with keys."""
        self.view_label.setText(view)
        self._fill([entry for entry in found if entry.keys])
        self.open_layer()
        self.setFocus()

    def _fill(self, shown):
        groups = {name: [] for name in GROUPS}
        for entry in shown:
            groups[entry.group].append(entry)
        groups = [(name, rows) for name, rows in groups.items() if rows]
        # Columns of about equal length, the groups in their order
        count = 3 if len(shown) > 24 else 2
        target = (len(shown) + 2 * len(groups)) / count
        columns, length = [[]], 0
        for name, rows in groups:
            if length >= target and len(columns) < count:
                columns.append([])
                length = 0
            columns[-1].append((name, rows))
            length += len(rows) + 2
        body = QWidget()
        row = QHBoxLayout(body)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(style.gap(28))
        self.rows = []
        for column in columns:
            stack = QVBoxLayout()
            stack.setSpacing(style.gap(14))
            for name, rows in column:
                stack.addWidget(self._group(name, rows))
            stack.addStretch(1)
            row.addLayout(stack)
        self.scroll.setWidget(body)

    def _group(self, name, rows):
        box = QWidget()
        grid = QGridLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(style.gap(16))
        grid.setVerticalSpacing(style.gap(4))
        heading = QLabel(name.upper())
        heading.setObjectName("heading")
        heading.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(heading, 0, 0, 1, 2)
        for place, entry in enumerate(rows, 1):
            label = QLabel(entry.name)
            # Dimmed while it can't be used here
            label.setEnabled(entry.enabled)
            grid.addWidget(label, place, 0)
            keys = QHBoxLayout()
            keys.setSpacing(style.gap(4))
            keys.addStretch(1)
            for key in entry.keys:
                cap = QLabel(key)
                cap.setObjectName("keycap")
                keys.addWidget(cap)
            grid.addLayout(keys, place, 1)
            self.rows.append((entry, label))
        grid.setColumnStretch(0, 1)
        return box

    def place(self):
        margin = style.px(32)
        room = self.rect().adjusted(margin, margin, -margin, -margin)
        body = self.scroll.widget()
        want = body.sizeHint() if body is not None else QSize()
        frame = self.card.layout().contentsMargins()
        width = min(room.width(), want.width() + frame.left() + frame.right() + style.px(12))
        height = min(room.height(), self.card.sizeHint().height() + want.height())
        self.card.setGeometry(
            QRect(0, 0, width, height).translated(
                room.left() + (room.width() - width) // 2,
                room.top() + (room.height() - height) // 2,
            )
        )

    def event(self, event):
        # Every key is this one's: the window's shortcuts do not run under it
        if event.type() == QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        if event.key() in (
            Qt.Key.Key_Shift,
            Qt.Key.Key_Control,
            Qt.Key.Key_Alt,
            Qt.Key.Key_Meta,
            Qt.Key.Key_AltGr,
        ):
            # Shift on the way to ?, which closes it too
            return
        self.close_layer()


# The entry a row of the palette shows
_ENTRY = Qt.ItemDataRole.UserRole


class _EntryDelegate(QStyledItemDelegate):
    """The action's name, why it can't be used if it can't, and its shortcut at the
    right."""

    def sizeHint(self, option, _index):
        return QSize(0, option.fontMetrics.height() + 2 * style.gap(7))

    def paint(self, painter, option, index):
        entry = index.data(_ENTRY)
        plain = QStyleOptionViewItem(option)
        self.initStyleOption(plain, index)
        plain.text = ""
        plain.icon = QIcon()
        widget = option.widget
        widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, plain, painter, widget)
        metrics = option.fontMetrics
        palette = option.palette
        rect = option.rect.adjusted(style.gap(10), 0, -style.gap(10), 0)
        painter.save()
        painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
        keys = "   ".join(entry.keys)
        if keys:
            painter.drawText(
                rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, keys
            )
            rect.setRight(rect.right() - metrics.horizontalAdvance(keys) - style.gap(16))
        group = QPalette.ColorGroup.Active if entry.enabled else QPalette.ColorGroup.Disabled
        painter.setPen(palette.color(group, QPalette.ColorRole.Text))
        name = metrics.elidedText(entry.name, Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
        if not entry.enabled and entry.why:
            rect.setLeft(rect.left() + metrics.horizontalAdvance(name) + style.gap(12))
            painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
            why = metrics.elidedText(entry.why, Qt.TextElideMode.ElideRight, rect.width())
            painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, why)
        painter.restore()


class CommandPalette(_Layer):
    """A field and the actions whose names have what is typed in it, each with its
    shortcut; Enter runs the one chosen. One that can't be used now is greyed, with
    why when that is known, and does not run."""

    def __init__(self, window):
        super().__init__(window, dim=0.35)
        self.card = QFrame(self)
        self.card.setObjectName("card")
        layout = QVBoxLayout(self.card)
        self.field = QLineEdit()
        self.field.setPlaceholderText("Type the name of an action")
        self.field.setClearButtonEnabled(True)
        self.field.textChanged.connect(self._filter)
        self.field.installEventFilter(self)
        layout.addWidget(self.field)
        self.list = QListWidget()
        self.list.setObjectName("palette")
        self.list.setItemDelegate(_EntryDelegate(self.list))
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setUniformItemSizes(True)
        self.list.itemClicked.connect(self.run)
        layout.addWidget(self.list)
        self.nothing = QLabel("No action has that in its name")
        self.nothing.setObjectName("placeholder")
        self.nothing.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.nothing)
        self._found = []

        def space():
            margin = style.gap(8)
            layout.setContentsMargins(margin, margin, margin, margin)
            layout.setSpacing(style.gap(6))

        style.follow_density(self.card, space)

    def open(self, found):
        """Offer the actions among `found` (from `entries`), but those that say `listed`
        False (the palette's own)."""
        self._found = [
            entry
            for entry in found
            if entry.action is not None and entry.action.property("listed") is not False
        ]
        self.field.clear()
        self._filter("")
        self.open_layer()
        self.field.setFocus()

    def shown(self):
        """The entries the list shows, in its order."""
        return [self.list.item(row).data(_ENTRY) for row in range(self.list.count())]

    def _filter(self, text):
        self.list.clear()
        for entry in matching(text, self._found):
            item = QListWidgetItem(entry.name)
            item.setData(_ENTRY, entry)
            item.setToolTip(entry.why if not entry.enabled else "")
            self.list.addItem(item)
        self.list.setCurrentRow(0)
        self.list.setVisible(self.list.count() > 0)
        self.nothing.setVisible(self.list.count() == 0)
        if self.isVisible():
            self.place()

    def place(self):
        rows = min(self.list.count(), _ROWS)
        if rows:
            row = self.list.sizeHintForRow(0)
            frame = 2 * self.list.frameWidth()
            self.list.setFixedHeight(rows * row + frame + 2 * style.gap(4))
        width = min(style.px(560), self.width() - 2 * style.px(16))
        self.card.setFixedWidth(width)
        self.card.adjustSize()
        self.card.move((self.width() - width) // 2, style.px(72))

    def run(self, item=None):
        """Run the chosen action (or `item`'s), closing the palette first. Returns
        whether it ran: one that can't be used now does not, and the palette stays."""
        item = item if isinstance(item, QListWidgetItem) else self.list.currentItem()
        if item is None:
            return False
        action = item.data(_ENTRY).action
        if not action.isEnabled():
            return False
        self.close_layer()
        action.trigger()
        return True

    def eventFilter(self, watched, event):
        if watched is self.field:
            if event.type() == QEvent.Type.ShortcutOverride:
                # Typing is for the field, not the window's shortcuts
                event.accept()
                return False
            if event.type() == QEvent.Type.KeyPress:
                return self._key(event)
            return False
        return super().eventFilter(watched, event)

    def _key(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape or (
            key == Qt.Key.Key_K and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.close_layer()
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.run()
            return True
        steps = {
            Qt.Key.Key_Down: 1,
            Qt.Key.Key_Up: -1,
            Qt.Key.Key_PageDown: _ROWS,
            Qt.Key.Key_PageUp: -_ROWS,
        }
        if key in steps and self.list.count():
            row = self.list.currentRow() + steps[key]
            self.list.setCurrentRow(max(0, min(self.list.count() - 1, row)))
            return True
        return False
