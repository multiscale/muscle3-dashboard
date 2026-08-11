import pytest

from muscle3_dashboard.visualization.base_state import BaseState, Dim, Variable


def test_variable_full_path_joins_source_and_path():
    var = Variable(source_name="equilibrium", path="ip", dimension=Dim.ZERO_D)
    assert var.full_path == "equilibrium/ip"


def test_base_state_extract_not_implemented():
    state = BaseState({})
    with pytest.raises(NotImplementedError):
        state.extract(None)


def test_base_state_automatic_extract_not_implemented():
    state = BaseState({})
    with pytest.raises(NotImplementedError):
        state.automatic_extract(None)


def test_extract_data_calls_automatic_extract_when_auto():
    calls = []

    class State(BaseState):
        def extract(self, payload):
            calls.append(("extract", payload))

        def automatic_extract(self, payload):
            calls.append(("automatic_extract", payload))

    state = State({}, auto=True)
    state.extract_data("payload")
    assert calls == [("automatic_extract", "payload"), ("extract", "payload")]


def test_extract_data_skips_automatic_extract_when_not_auto():
    calls = []

    class State(BaseState):
        def extract(self, payload):
            calls.append(("extract", payload))

        def automatic_extract(self, payload):
            calls.append(("automatic_extract", payload))

    state = State({}, auto=False)
    state.extract_data("payload")
    assert calls == [("extract", "payload")]
