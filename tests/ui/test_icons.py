"""Icons come from the icon theme, as filehouse's do: the symbolic version first."""

from PySide6.QtGui import QIcon

from lumina.integration import style


def test_the_symbolic_version_of_an_icon_is_asked_for_first(monkeypatch):
    asked = []
    has = {"preferences-system", "preferences-system-symbolic", "configure"}

    def has_theme_icon(name):
        asked.append(name)
        return name in has

    monkeypatch.setattr(QIcon, "hasThemeIcon", staticmethod(has_theme_icon))
    monkeypatch.setattr(QIcon, "fromTheme", staticmethod(lambda name: name))

    assert style._theme_icon(("preferences-system", "configure")) == "preferences-system-symbolic"
    assert asked == ["preferences-system-symbolic"]

    # Without a symbolic one, the icon itself, before the next name
    has.discard("preferences-system-symbolic")
    assert style._theme_icon(("preferences-system", "configure")) == "preferences-system"
