import holoviews as hv
import panel as pn

from muscle3_dashboard.visualization.base_plotter import BasePlotter
from muscle3_dashboard.visualization.base_state import BaseState, Dim


class _State(BaseState):
    def extract(self, payload):
        pass


class _Plotter(BasePlotter):
    def get_dashboard(self):
        return pn.Column()


def test_plot_empty_zero_d_is_a_curve():
    plotter = _Plotter(_State({}))
    element = plotter.plot_empty("v", Dim.ZERO_D)
    assert isinstance(element, hv.Curve)


def test_plot_empty_two_d_is_a_quadmesh():
    plotter = _Plotter(_State({}))
    element = plotter.plot_empty("v", Dim.TWO_D)
    assert isinstance(element, hv.QuadMesh)
