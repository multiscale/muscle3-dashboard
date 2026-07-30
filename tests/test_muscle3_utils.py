from unittest.mock import Mock

from ymmsl.v0_2 import Operator

from muscle3_dashboard.muscle3_utils import get_port_list, get_setting_optional


def test_get_setting_optional_returns_value_when_present():
    instance = Mock()
    instance.get_setting.return_value = "my_value"
    assert get_setting_optional(instance, "some_setting") == "my_value"


def test_get_setting_optional_returns_none_when_missing_and_no_default():
    instance = Mock()
    instance.get_setting.side_effect = KeyError("some_setting")
    assert get_setting_optional(instance, "some_setting") is None


def test_get_setting_optional_returns_default_when_missing():
    instance = Mock()
    instance.get_setting.side_effect = KeyError("some_setting")
    assert (
        get_setting_optional(instance, "some_setting", default="fallback") == "fallback"
    )


def test_get_port_list_filters_to_connected_and_sorts():
    instance = Mock()
    instance.list_ports.return_value = {
        Operator.S: ["c_in", "a_in", "b_in"],
    }
    instance.is_connected.side_effect = lambda port: port != "b_in"
    assert get_port_list(instance, Operator.S) == ["a_in", "c_in"]


def test_get_port_list_returns_empty_for_unknown_operator():
    instance = Mock()
    instance.list_ports.return_value = {Operator.S: ["a_in"]}
    instance.is_connected.return_value = True
    assert get_port_list(instance, Operator.O_F) == []
