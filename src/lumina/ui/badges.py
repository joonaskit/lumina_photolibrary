"""The small badges in the top left corner of a thumbnail: how many photos a stack holds
and how many virtual copies a photo has, side by side where the stars and the flag are
not."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter

from lumina.integration import style


def stack_text(stacked):
    """What the badge of a photo in a stack says: its size while collapsed, else its
    place in it. `stacked` is the cell's (place, size, collapsed)."""
    place, size, collapsed = stacked
    return str(size) if collapsed else f"{place}/{size}"


def pill_rect(photo_rect, text, left=0.0):
    """Where the badge with `text` is on a photo (`left` pixels in from its top left
    corner), or None when the thumbnail is too small for one."""
    height = style.px(20)
    if photo_rect.width() < 3 * height or photo_rect.height() < 3 * height:
        return None
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
