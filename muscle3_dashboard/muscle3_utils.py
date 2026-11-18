"""Small libmuscle helpers with no domain coupling of their own -- reused by
:mod:`.recorder.actor` and available to any actor that wants them.
"""

from libmuscle import Instance
from ymmsl.v0_2 import Operator


def get_port_list(instance: Instance, operator: Operator) -> list[str]:
    """Sorted list of `instance`'s connected ports for one operator."""
    total_port_list = instance.list_ports().get(operator, [])
    return sorted(port for port in total_port_list if instance.is_connected(port))
