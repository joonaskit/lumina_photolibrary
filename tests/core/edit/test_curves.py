import numpy as np
import pytest

from lumina.core.edit import curves


def test_identity_is_a_straight_line():
    table = curves.lut(curves.IDENTITY)

    assert np.allclose(table, np.linspace(0, 1, curves.LUT_SIZE), atol=1e-6)


def test_the_curve_goes_through_its_points():
    points = ((0, 0.05), (0.25, 0.2), (0.5, 0.6), (0.8, 0.85), (1, 0.95))

    values = curves.evaluate(points, [p[0] for p in points])

    assert np.allclose(values, [p[1] for p in points], atol=1e-9)


def test_rising_points_give_a_rising_curve_that_never_overshoots():
    # A steep S: a natural spline overshoots this, below 0.1 and above 0.9
    points = ((0, 0), (0.3, 0.1), (0.35, 0.9), (1, 1))
    table = curves.lut(points)

    assert np.all(np.diff(table) >= -1e-7)
    x = np.linspace(0, 1, curves.LUT_SIZE)
    between = (x > 0.3) & (x < 0.35)
    assert table[between].min() >= 0.1 - 1e-7 and table[between].max() <= 0.9 + 1e-7


def test_a_point_dragged_below_its_neighbour_makes_no_wild_swing():
    # Not monotone: still no value outside the range of the two points around it
    points = ((0, 0), (0.4, 0.7), (0.6, 0.3), (1, 1))
    x = np.linspace(0.4, 0.6, 200)

    values = curves.evaluate(points, x)

    assert values.min() >= 0.3 - 1e-9 and values.max() <= 0.7 + 1e-9


def test_points_are_cleaned_up():
    assert curves.normalize([(0.5, 0.7)]) == curves.IDENTITY
    assert curves.normalize([]) == curves.IDENTITY
    assert curves.normalize([(0.2, 0.1), (0.8, 0.9)]) == (
        (0, 0.1),
        (0.2, 0.1),
        (0.8, 0.9),
        (1, 0.9),
    )
    assert curves.normalize([(-1, -2), (2, 3), (0.5, 0.5)]) == ((0, 0), (0.5, 0.5), (1, 1))
    # One point per x, the last wins; rubbish is dropped
    assert curves.normalize([(0, 0), (0.5, 0.2), (0.5, 0.4), ("x", 1), (1, 1)]) == (
        (0, 0),
        (0.5, 0.4),
        (1, 1),
    )


@pytest.mark.parametrize("points", [((0, 0), (1, 1)), ((0, 0.2), (1, 0.8))])
def test_two_points_are_a_straight_line(points):
    x = np.linspace(0, 1, 11)

    assert np.allclose(curves.evaluate(points, x), points[0][1] + x * (points[1][1] - points[0][1]))
