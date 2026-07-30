"""Small libmuscle helpers with no domain coupling of their own -- reused by
:mod:`.recorder.actor` and available to any actor that wants them.
"""

from typing import Optional, TypeVar, cast

from libmuscle import Instance
from ymmsl.v0_2 import Operator, SettingValue

TSetting = TypeVar("TSetting", bound=SettingValue)


def get_setting_optional(
    instance: Instance,
    setting_name: str,
    default: Optional[TSetting] = None,
) -> Optional[TSetting]:
    """Read an optional setting from `instance`.

    libmuscle's Instance.get_setting(default=...) cannot distinguish "no
    default" from "default is None", so it re-raises KeyError even when
    default=None is passed explicitly. This wraps it correctly.
    """
    try:
        return cast(TSetting, instance.get_setting(setting_name))
    except KeyError:
        return default


def get_port_list(instance: Instance, operator: Operator) -> list[str]:
    """Sorted list of `instance`'s connected ports for one operator."""
    total_port_list = instance.list_ports().get(operator, [])
    return sorted(port for port in total_port_list if instance.is_connected(port))
