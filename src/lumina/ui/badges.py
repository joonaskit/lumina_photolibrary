"""The small badges in the top left corner of a thumbnail: how many photos a stack holds
(on a pill) and how many virtual copies a photo has (on a diamond), side by side where
the stars and the flag are not."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainter, QPolygonF

from lumina.core.dbtypes import HDR, PANORAMA
from lumina.integration import style


def stack_text(stacked):
    """What the badge of a photo in a stack says: its size while collapsed, else its
    place in it. `stacked` is the cell's (place, size, collapsed)."""
    place, size, collapsed = stacked
    return str(size) if collapsed else f"{place}/{size}"


def _room(photo_rect):
    """Whether a photo's thumbnail has room for badges: it is not a small one (judged by
    its longer side, which follows the grid's size, not by the photo's shape: a
    panorama's is short and wide), and a badge fits inside its shorter side."""
    height = style.px(20)
    longer = max(photo_rect.width(), photo_rect.height())
    shorter = min(photo_rect.width(), photo_rect.height())
    return longer >= 3 * height and shorter >= height + 2 * style.px(4)


def anchor_rect(photo_rect, cell_rect):
    """The rectangle whose corners the badges sit in: the photo's, or the cell's when the
    photo is too short for a row of badges at the top and another at the bottom (a
    panorama's strip in a square cell) but the cell has room. A cell too small for badges
    stays the photo's, so they are left out as before."""
    need = style.px(20) + style.px(22) + 3 * style.px(4)
    thin = min(photo_rect.width(), photo_rect.height()) < need
    if thin and min(cell_rect.width(), cell_rect.height()) >= need:
        return cell_rect
    return photo_rect


def pill_rect(photo_rect, text, left=0.0):
    """Where the badge with `text` is on a photo (`left` pixels in from its top left
    corner), or None when the thumbnail is too small for one."""
    if not _room(photo_rect):
        return None
    height = style.px(20)
    width = height + style.px(6) * (len(text) - 1)
    inset = style.px(4)
    return QRectF(photo_rect.left() + inset + left, photo_rect.top() + inset, width, height)


def paint_pill(painter, photo_rect, text, left=0.0, stacked=False):
    """`text` on a rounded card of the window's card colour, so it shows on any photo,
    `left` pixels in from the photo's top left corner. A `stacked` one has a second card
    peeping out behind it. Returns where the badge ends (for the next to start there), or
    `left` when the thumbnail is too small for it."""
    card = pill_rect(photo_rect, text, left)
    if card is None:
        return left
    height, width, inset = card.height(), card.width(), style.px(4)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    background = style.layer("card")
    background.setAlphaF(0.9)
    painter.setBrush(background)
    behind = style.px(3)
    if stacked:
        painter.setOpacity(0.6)
        painter.drawRoundedRect(card.translated(behind, behind), height / 2, height / 2)
        painter.setOpacity(1.0)
    painter.drawRoundedRect(card, height / 2, height / 2)
    font = painter.font()
    font.setPixelSize(max(1, round(height * 0.6)))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(style.color("textPrimary"))
    painter.drawText(card, Qt.AlignmentFlag.AlignCenter, text)
    painter.restore()
    return left + width + inset + (behind if stacked else 0)


def diamond_rect(photo_rect, text, left=0.0):
    """Where the virtual copies' badge is on a photo (`left` pixels in from its top left
    corner), or None when the thumbnail is too small for one. A diamond is wider than a
    pill for the same text, as its corners hold none."""
    size = style.px(26) + style.px(5) * (len(text) - 1)
    # Too small a thumbnail is judged as for the pill, whatever the number
    if not _room(photo_rect):
        return None
    inset = style.px(4)
    return QRectF(photo_rect.left() + inset + left, photo_rect.top() + inset, size, size)


def paint_diamond(painter, photo_rect, text, left=0.0):
    """`text` on a diamond of the window's card colour, unlike the stack's pill. Returns
    where the badge ends (for the next to start there), or `left` when the thumbnail is
    too small for it."""
    card = diamond_rect(photo_rect, text, left)
    if card is None:
        return left
    centre = card.center()
    half_w, half_h = card.width() / 2, card.height() / 2
    shape = QPolygonF(
        [
            QPointF(centre.x(), card.top()),
            QPointF(card.right(), centre.y()),
            QPointF(centre.x(), card.bottom()),
            QPointF(card.left(), centre.y()),
        ]
    )
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    background = style.layer("card")
    background.setAlphaF(0.9)
    painter.setBrush(background)
    painter.drawPolygon(shape)
    font = painter.font()
    font.setPixelSize(max(1, round(card.height() * 0.4)))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(style.color("textPrimary"))
    inner = QRectF(0, 0, half_w * 1.1, half_h * 1.1)
    inner.moveCenter(centre)
    painter.drawText(inner, Qt.AlignmentFlag.AlignCenter, text)
    painter.restore()
    return left + card.width() + style.px(4)


def derived_text(kind):
    """What the badge of a merged photo says: "HDR" or "PANO"."""
    return {HDR: "HDR", PANORAMA: "PANO"}[kind]


def paint_stack_cue(painter, photo_rect):
    """Two more photos' edges peeping out behind a collapsed stack's top photo, below and
    to the right (the cell has room there, not above), so the cell looks like a pile."""
    step = style.px(4)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    for depth in (2, 1):
        sheet = QRectF(photo_rect).translated(depth * step, depth * step)
        painter.setPen(style.overlay(0.5))
        painter.setBrush(style.layer("raised"))
        painter.drawRoundedRect(sheet, style.px(2), style.px(2))
    painter.restore()
