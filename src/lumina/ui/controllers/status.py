"""The status bar's line: how many photos the grid shows and why, or of the photo looked
at, where it is in the grid, its flag, stars, keywords, zoom and how it was taken."""

from PySide6.QtCore import QObject

from lumina.qt_models.library_model import SIMILAR
from lumina.ui import stars
from lumina.ui.window_common import _DEVELOP, _VIEWER, FLAG_NAMES, describe_photo


class StatusLine(QObject):
    def __init__(self, label, nav, model, service, search, parent=None):
        """`label` is the QLabel it writes in; `search` the SearchService, or None."""
        super().__init__(parent)
        self.label = label
        self.nav = nav
        self.model = model
        self.service = service
        self.search = search
        # What it says of the zoom
        self._zoom_shown = None
        nav.status_changed.connect(self.update)

    def zoom_changed(self, *_args):
        # Not on every move of a drag: the status bar reads the library
        if self._zoom_state() != self._zoom_shown:
            self.update()

    def _zoom_loading(self, view):
        """Whether the image shown is short of what the zoom shows."""
        if view is self.nav.viewer.view:
            return view.needs_full()
        # Zoomed in the Develop view, the photo's own pixels come after the screen-sized image
        return view.is_zoomed() and not view.has_detail()

    def _zoom_state(self):
        view = self.nav.zoom_view()
        return (view.is_zoomed(), view.zoom(), self._zoom_loading(view))

    def update(self, *_args):
        self._zoom_shown = self._zoom_state()
        nav, model = self.nav, self.model
        count = model.rowCount()
        row = nav.viewed_row()
        if not self.service.folders():
            text = ""
        elif nav.page() in (_VIEWER, _DEVELOP) and row is not None:
            details = describe_photo(self.service.photo_details(nav.viewed_id))
            where = model.path(row) or f"{model.name(row)} (not connected)"
            flag = FLAG_NAMES.get(model.flag(row))
            rated = stars.text(model.rating(row))
            tagged = ", ".join(model.keywords(row))
            tagged = f"Keywords: {tagged}" if tagged else None
            zoom = None
            view = nav.zoom_view()
            if view.is_zoomed():
                zoom = f"{round(view.zoom() * 100)} %"
                if self._zoom_loading(view):
                    zoom += ", loading full size…" if view is nav.viewer.view else ", rendering…"
            damaged = "Raw data may be damaged" if model.damage(row) else None
            versions = None
            numbers = [0, *self.service.copies(nav.viewed_id)]
            if len(numbers) > 1:
                place = numbers.index(nav.viewed_copy) + 1 if nav.viewed_copy in numbers else 1
                versions = f"Edit {place} of {len(numbers)}"
            parts = (
                f"{row + 1} of {count}",
                versions,
                zoom,
                flag,
                rated,
                tagged,
                damaged,
                details,
                where,
            )
            text = "  ·  ".join(part for part in parts if part)
        else:
            text = f"{count} photo" + ("" if count == 1 else "s")
            search = model.search
            if search is not None and model.is_loading():
                text = "Searching…"
                if search.kind != SIMILAR and self.search and not self.search.is_text_loaded():
                    text = "Loading the search model…"
            elif search is not None and search.kind == SIMILAR:
                text = (
                    f"{text} like {search.label}"
                    if count
                    else f"{search.label} is not indexed for search yet"
                )
            elif search is not None:
                verb = "matches" if count == 1 else "match"
                text = (
                    f"{text} {verb} “{search.label}”"
                    if count
                    else f"No photos match “{search.label}”"
                )
            hidden = model.hidden_count()
            if hidden and not model.is_loading():
                text += f"  ·  {hidden} hidden by flags"
            if search is not None and self.search.is_indexing():
                text += "  ·  more as indexing goes on"
        self.label.setText(text)
