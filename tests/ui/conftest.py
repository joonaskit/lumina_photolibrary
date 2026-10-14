import pytest

from lumina.integration import style, theme


@pytest.fixture(autouse=True)
def themed_app(qapp):
    style.apply(qapp, theme.load_theme("/nonexistent/theme.json"))
    return qapp


@pytest.fixture(autouse=True)
def fresh_settings():
    """Nothing remembered from an earlier test."""
    from lumina.ui import settings

    settings.clear()
    yield
    settings.clear()


@pytest.fixture(scope="session", autouse=True)
def stop_workers():
    from lumina.qt_models import workers

    yield
    workers.shutdown()
