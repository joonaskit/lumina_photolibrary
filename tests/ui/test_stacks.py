"""Stacks of photos in the grid: one cell while collapsed, the filters look at the top,
the viewer skips what is hidden."""

import os

from conftest import TIMEOUT
from PySide6.QtWidgets import QStyleOptionViewItem
from test_flags import choose, names, open_window

from lumina.core.dbtypes import PICKED
from lumina.qt_models.library_model import STACK_ROLE
from lumina.ui import badges
from lumina.ui.main_window import _VIEWER


def wait_for_rows(qtbot, window, count):
    qtbot.waitUntil(lambda: window.model.rowCount() == count, timeout=TIMEOUT)


def stack_all(qtbot, window, rows=(0, 1, 2)):
    choose(window, *rows)
    window.stacking.stack_photos()


def test_a_collapsed_stack_is_one_cell_with_its_top_and_its_size(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first = names(window)[0]
    choose(window, 0, 1)

    window.stacking.stack_photos()
    wait_for_rows(qtbot, window, 2)

    assert names(window)[0] == first
    assert window.model.data(window.model.index(0), STACK_ROLE) == (1, 2, True)
    assert window.model.data(window.model.index(1), STACK_ROLE) is None
    assert window.status.text().startswith("2 photos")


def test_expanding_shows_the_members_after_the_top_and_collapsing_hides_them(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    before = names(window)
    stack_all(qtbot, window)
    wait_for_rows(qtbot, window, 1)
    choose(window, 0)

    window.stacking.toggle_stacks()
    wait_for_rows(qtbot, window, 3)

    assert names(window) == before
    assert [window.model.data(window.model.index(r), STACK_ROLE)[0] for r in range(3)] == [1, 2, 3]
    window.stacking.toggle_stacks()
    wait_for_rows(qtbot, window, 1)


def test_a_new_top_is_the_cell_a_collapsed_stack_shows(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    before = names(window)
    stack_all(qtbot, window)
    wait_for_rows(qtbot, window, 1)
    choose(window, 0)
    window.stacking.toggle_stacks()
    wait_for_rows(qtbot, window, 3)
    choose(window, 2)
    assert window.stack_top_action.isEnabled()

    window.stacking.set_stack_top()
    qtbot.waitUntil(lambda: names(window)[0] == before[2], timeout=TIMEOUT)
    choose(window, 0)
    window.stacking.toggle_stacks()

    wait_for_rows(qtbot, window, 1)
    assert names(window) == [before[2]]


def test_unstacking_gives_every_photo_its_own_cell_again(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    before = names(window)
    stack_all(qtbot, window)
    wait_for_rows(qtbot, window, 1)
    choose(window, 0)

    window.stacking.unstack_photos()

    wait_for_rows(qtbot, window, 3)
    assert names(window) == before
    assert window.model.data(window.model.index(0), STACK_ROLE) is None


def test_a_stack_is_shown_only_while_its_top_passes_the_filters(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    before = names(window)
    stack_all(qtbot, window, (0, 1))
    wait_for_rows(qtbot, window, 2)
    # The second photo, not the top, is picked
    choose(window, 0)
    window.stacking.toggle_stacks()
    wait_for_rows(qtbot, window, 3)
    choose(window, 1)
    window.culling.flag_photos(PICKED)
    qtbot.waitUntil(lambda: window.model.flag(1) == PICKED, timeout=TIMEOUT)

    window.browser.set_flag_filter(PICKED)

    # Picked photos only: the stack's top is not one, so the stack is not shown
    assert names(window) == []
    window.browser.set_flag_filter(None)
    assert names(window) == before


def test_an_expanded_stack_shows_only_the_members_that_pass(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    before = names(window)
    stack_all(qtbot, window, (0, 1, 2))
    wait_for_rows(qtbot, window, 1)
    choose(window, 0)
    window.stacking.toggle_stacks()
    wait_for_rows(qtbot, window, 3)
    choose(window, 0)
    window.culling.flag_photos(PICKED)
    choose(window, 2)
    window.culling.flag_photos(PICKED)
    qtbot.waitUntil(
        lambda: window.model.flag(0) == PICKED and window.model.flag(2) == PICKED,
        timeout=TIMEOUT,
    )

    window.browser.set_flag_filter(PICKED)

    assert names(window) == [before[0], before[2]]


def test_stepping_in_the_viewer_skips_the_photos_a_collapsed_stack_hides(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    before = names(window)
    stack_all(qtbot, window, (0, 1))
    wait_for_rows(qtbot, window, 2)

    window.nav.show_photo(0)
    assert window.pages.currentIndex() == _VIEWER
    window.nav.step(1)

    assert os.path.basename(window.nav.viewed_path()) == before[2]
    assert "2 of 2" in window.status.text()


def test_a_stack_made_in_the_grid_survives_a_new_window(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    stack_all(qtbot, window)
    wait_for_rows(qtbot, window, 1)
    window.close()

    again = open_window_again(qtbot, service)

    assert again.model.rowCount() == 1


def open_window_again(qtbot, service):
    from lumina.ui.main_window import MainWindow

    window = MainWindow(service)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(
        lambda: not window.model.is_loading() and window.model.rowCount(), timeout=TIMEOUT
    )
    return window


def test_stacking_needs_two_photos_chosen(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    choose(window, 0)
    assert not window.stack_action.isEnabled()
    choose(window, 0, 1)
    assert window.stack_action.isEnabled()
    assert not window.unstack_action.isEnabled()


def test_a_photo_is_taken_out_of_an_expanded_stack(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    before = names(window)
    stack_all(qtbot, window)
    wait_for_rows(qtbot, window, 1)
    choose(window, 0)
    window.stacking.toggle_stacks()
    wait_for_rows(qtbot, window, 3)
    photo = window.model.photo_id(1)
    choose(window, 1)
    assert window.leave_stack_action.isEnabled()

    window.stacking.leave_stack()

    def left():
        row = window.model.row_of(photo)
        return window.model.data(window.model.index(row), STACK_ROLE) is None

    qtbot.waitUntil(left, timeout=TIMEOUT)
    # The two left are still a stack, shown together after the top
    others = [r for r in range(3) if window.model.data(window.model.index(r), STACK_ROLE)]
    assert [window.model.name(r) for r in others] == [before[0], before[2]]
    assert window.model.rowCount() == 3


def test_a_click_on_the_badge_opens_and_closes_a_stack(qtbot, service, drive):
    from PySide6.QtCore import QPoint, Qt

    window = open_window(qtbot, service, drive / "Photos")
    window.resize(900, 600)
    stack_all(qtbot, window)
    wait_for_rows(qtbot, window, 1)
    qtbot.waitUntil(
        lambda: window.grid.visualRect(window.model.index(0)).isValid(), timeout=TIMEOUT
    )
    index = window.model.index(0)

    def badge_point():
        option = QStyleOptionViewItem()
        window.grid.initViewItemOption(option)
        option.rect = window.grid.visualRect(index)
        option.widget = window.grid
        photo = window._delegate.photo_rect(option, index)
        card = badges.pill_rect(photo, badges.stack_text(index.data(STACK_ROLE)))
        return card.center().toPoint()

    qtbot.mouseClick(window.grid.viewport(), Qt.MouseButton.LeftButton, pos=badge_point())
    wait_for_rows(qtbot, window, 3)
    index = window.model.index(0)
    qtbot.mouseClick(window.grid.viewport(), Qt.MouseButton.LeftButton, pos=badge_point())
    wait_for_rows(qtbot, window, 1)
    # Elsewhere on the cell chooses it and changes nothing
    far = window.grid.visualRect(index).bottomRight() - QPoint(2, 2)
    qtbot.mouseClick(window.grid.viewport(), Qt.MouseButton.LeftButton, pos=far)
    assert window.model.rowCount() == 1


def give_vectors(service, window, vectors):
    """Store a search vector for each photo shown, in the order of the grid."""
    from lumina.core import db as dbm
    from lumina.core import embed

    with dbm.Library(service.path) as library:
        library.write_embeddings(
            embed.MODEL.name,
            [
                (row.content_id, embed.to_blob(vector))
                for row, vector in zip(window.model.photo_rows(), vectors, strict=True)
            ],
        )


def unit(*head):
    from lumina.core import embed

    vector = [0.0] * embed.DIMENSIONS
    vector[: len(head)] = head
    return vector


def test_stack_bursts_stacks_photos_that_look_alike_and_leaves_the_others(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    first = names(window)[:2]
    give_vectors(service, window, [unit(1), unit(1), unit(0, 1)])

    window.stacking.stack_bursts()

    wait_for_rows(qtbot, window, 2)
    qtbot.waitUntil(lambda: "Stacked" in window.statusBar().currentMessage(), timeout=TIMEOUT)
    assert names(window)[0] == first[0]
    assert window.model.data(window.model.index(0), STACK_ROLE)[1] == 2
    assert window.model.data(window.model.index(1), STACK_ROLE) is None
    assert window.statusBar().currentMessage() == "Stacked 1 burst"


def test_stack_bursts_without_vectors_stacks_nothing(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")

    window.stacking.stack_bursts()

    qtbot.waitUntil(
        lambda: window.statusBar().currentMessage() == "No bursts found", timeout=TIMEOUT
    )
    assert window.model.rowCount() == 3


def test_stack_bursts_leaves_a_stack_made_by_hand_as_it_is(qtbot, service, drive):
    window = open_window(qtbot, service, drive / "Photos")
    give_vectors(service, window, [unit(1), unit(1), unit(1)])
    stack_all(qtbot, window, (0, 1))
    wait_for_rows(qtbot, window, 2)

    window.stacking.stack_bursts()

    # The third alone is no burst: the other two are stacked already
    wait_for_rows(qtbot, window, 2)
    qtbot.waitUntil(
        lambda: window.statusBar().currentMessage() == "No bursts found", timeout=TIMEOUT
    )
    assert window.model.data(window.model.index(0), STACK_ROLE)[1] == 2


def test_a_wide_photo_on_top_of_a_stack_has_its_badge():
    from PySide6.QtCore import QRect

    from lumina.integration import style

    side = style.px(200)
    # A panorama's thumbnail: as wide as any in the grid, but short
    wide = QRect(0, 0, side, side // 4)
    assert badges.pill_rect(wide, "20") is not None
    assert badges.diamond_rect(wide, "2") is not None
    # A small grid's thumbnails have none, whatever their shape
    small = QRect(0, 0, style.px(50), style.px(40))
    assert badges.pill_rect(small, "20") is None
    # Nor one too short for a badge to fit inside it
    strip = QRect(0, 0, side, style.px(20))
    assert badges.pill_rect(strip, "20") is None


def test_a_panoramas_badges_sit_in_the_cell_when_its_strip_is_too_thin():
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QPainter

    from lumina.integration import style
    from lumina.ui import damage, flags, stars

    side = style.px(150)
    cell = QRectF(0, 0, side, side)
    # A 6:1 panorama's strip in a square cell: too short for a badge inside it
    strip = QRectF(0, (side - side / 6) / 2, side, side / 6)
    assert badges.anchor_rect(strip, cell) == cell
    # An ordinary photo keeps its own corners
    photo = QRectF(0, side / 8, side, side * 3 / 4)
    assert badges.anchor_rect(photo, cell) == photo
    # A small grid's cell has no room either, so nothing moves
    small = QRectF(0, 0, style.px(20), style.px(20))
    assert badges.anchor_rect(QRectF(0, 5, 20, 3), small) == QRectF(0, 5, 20, 3)

    blank = QImage(int(side), int(side), QImage.Format.Format_ARGB32)
    blank.fill(0)

    def paints(draw):
        image = QImage(blank)
        painter = QPainter(image)
        draw(painter, badges.anchor_rect(strip, cell))
        painter.end()
        return image != blank

    assert paints(lambda p, r: flags.paint_badge(p, r, 1))
    assert paints(lambda p, r: stars.paint_stars(p, r, 3))
    assert paints(damage.paint_badge)
    # Painted on the strip itself, as before the fix, they would not show
    image = QImage(blank)
    painter = QPainter(image)
    flags.paint_badge(painter, strip, 1)
    painter.end()
    assert image == blank


def test_a_merged_photo_has_its_badge_in_the_grid_and_its_kind_in_the_status_line(
    qtbot, service, drive
):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter

    from lumina.core import db as dbm
    from lumina.qt_models.library_model import DERIVED_ROLE

    window = open_window(qtbot, service, drive / "Photos")
    for row in range(3):
        window.model.data(window.model.index(row), Qt.ItemDataRole.DecorationRole)
    qtbot.waitUntil(
        lambda: all(window.model.thumbnail_pixmap(row) is not None for row in range(3)),
        timeout=TIMEOUT,
    )
    window.grid.clearSelection()

    def cell(row):
        rect = window.grid.visualRect(window.model.index(row))
        image = QImage(rect.size(), QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        painter.translate(-rect.topLeft())
        option = QStyleOptionViewItem()
        window.grid.initViewItemOption(option)
        option.rect = rect
        window.grid.itemDelegate().paint(painter, option, window.model.index(row))
        painter.end()
        return image

    before = [cell(row) for row in range(3)]
    service.settle()
    with dbm.Library(service.path) as library:
        target, *others = library.photos()
        sources = [dbm.Source(p.volume_key, p.path) for p in others]
        job = library.add_derived_job(dbm.PANORAMA, "{}", sources, library.folders()[0].id)
        library.set_derived_result(library.job(job).derived_id, target.volume_key, target.path)

    def kinds():
        return [
            window.model.data(window.model.index(r), DERIVED_ROLE)
            for r in range(window.model.rowCount())
        ]

    window.model.refresh()
    qtbot.waitUntil(lambda: dbm.PANORAMA in kinds(), timeout=TIMEOUT)
    window.grid.clearSelection()

    assert kinds().count(dbm.PANORAMA) == 1 and kinds().count(None) == 2
    row = kinds().index(dbm.PANORAMA)
    # Only the merged photo's cell changed, and it stays so when the rows are read again
    after = [cell(r) for r in range(3)]
    assert [a != b for a, b in zip(after, before, strict=True)] == [r == row for r in range(3)]
    window.model.refresh()
    qtbot.waitUntil(lambda: not window.model.is_loading(), timeout=TIMEOUT)
    assert kinds().index(dbm.PANORAMA) == row

    window.nav.show_photo(row)
    assert "Panorama of 2 photos" in window.status.text()
    assert badges.derived_text(dbm.PANORAMA) == "PANO"
    assert badges.derived_text(dbm.HDR) == "HDR"
