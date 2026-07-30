# Panel dashboard for MUSCLE3 simulations
muscle_dashboard is a Panel based dashboard for log parsing and debugging of MUSCLE3 simulations.

# Installation
Quick developer installation guide

```bash
git clone git@github.com:multiscale/muscle3-dashboard.git
cd muscle3-dashboard
python3 -m venv ./venv
. venv/bin/activate
pip install -e .[dev]
pytest
```

The per-run **simulation graph** is drawn from the run's `configuration.ymmsl`
by [`ymmsl2svg`](https://github.com/multiscale/ymmsl2svg), an optional
dependency. Install it with the `graph` extra (without it the rest of the
dashboard works and the graph card is simply hidden):

```bash
pip install -e .[dev,graph]
```

# How to use
```bash
# make sure your virtual environment is activated
muscle_dashboard path/to/my/muscle/simulation/workdir
```

# Recording your own actor's data

Any MUSCLE3 actor can gain a live/replay Recorder tab in the dashboard by
writing its traffic through `muscle3_dashboard.recorder` -- no dashboard-side
configuration needed, since a recorder is recognised purely by its on-disk
footprint.

```bash
pip install muscle3-dashboard[recording]
```

Bring two things: a `deserialize(bytes) -> your_message` function per port,
and a `config` file defining `extract(message) -> dict[str, xarray.Dataset]`
(or a `State` class -- see `muscle3_dashboard.visualization.base_state` --
so the same file can double as a `Plotter` for live viewing). Everything
else is generic:

```python
import functools
from pathlib import Path

from libmuscle import Instance, InstanceFlags
from ymmsl.v0_2 import Operator

from muscle3_dashboard.recorder.collection import RecorderCollection
from muscle3_dashboard.recorder.zarr_recorder import ZarrRecorder


def deserialize(port: str, data: bytes) -> "MyMessage":
    ...  # bytes -> your own message type


def main() -> None:
    instance = Instance(flags=InstanceFlags.USES_CHECKPOINT_API)
    while instance.reuse_instance():
        ports = [
            p for p in instance.list_ports().get(Operator.S, [])
            if instance.is_connected(p)
        ]
        collection = RecorderCollection(
            store_path=Path(instance.get_setting("store_path", "str")),
            config=Path(instance.get_setting("config", "str")),
            deserializers={p: functools.partial(deserialize, p) for p in ports},
            make_recorder=ZarrRecorder,
        )
        for port in ports:
            collection.handle(port, instance.receive(port))
        collection.close()
```

`RecorderCollection` writes one live-tailable Zarr store per port per
outer-loop iteration, snapshots `config` next to the data for provenance,
and supports checkpoint/resume via `collection.get_state()`/
`restore_state()`. For the full production pattern -- multiple ports
drained independently, end-of-stream handling, and checkpointing wired up
-- see `imas_muscle3.actors.recorder_component` for a complete reference
implementation.

# Legal

Copyright 2026 ITER Organization. The code in this repository is licensed under the
[Apache-2.0 license](LICENSE.txt)

# m3dash: all your runs behind one SSH forward

`m3dash` finds your MUSCLE3 runs (via SLURM, local `muscle_manager`
processes, and a filesystem scan of the run roots you give it, default
the current directory) and serves a landing page that lists them plus a
per-run dashboard for each, on a single per-user unix socket:
`~/.m3dash.sock` (mode 0600).

On your own machine, add to `~/.ssh/config`. The `%r` token expands to
the remote username, so the same line works for every user (ITER SDCC
example):

```
Host sdcc1
    HostName sdcc1.iter.org
    LocalForward 127.0.0.1:4333 /home/ITER/%r/.m3dash.sock
    ExitOnForwardFailure no
```

Then http://localhost:4333 is a permanent bookmark for the runs index.
Pin one specific login node: unix sockets are host-local even on a
shared filesystem, so sshd and m3dash must be on the same machine.

**Start the server** where the environment is set up:

```bash
module load IMAS-MUSCLE3        # whatever puts m3dash on PATH
m3dash socket ~/runs ~/pds      # roots to scan; default: current dir
```

`m3dash socket` runs in the foreground. Note that `m3dash` often lives
behind `module load`, which a non-interactive ssh shell does *not* run.

## TCP access

On a desktop session running on the node itself (e.g. NoMachine),
`m3dash open` serves on a loopback TCP port and opens a browser at the
page. Pick the port with `--port`; the default is a deterministic
per-user port (`20000 + uid % 10000`).

A loopback TCP port is also the fallback where sshd prohibits
unix-socket forwarding (the symptom is `administratively prohibited:
open failed` while normal `-L` port forwards work): run `m3dash open
--no-open-browser` and forward the port with a plain `LocalForward
127.0.0.1:<port> 127.0.0.1:<port>`. Note that unlike the 0600 socket, a
loopback port is connectable by other users on the same node.

## Commands

Each command takes the run roots to scan as positional arguments,
defaulting to the current directory:

* `m3dash socket [ROOTS...]` — for remote access: serve on a unix socket
  reached through your SSH forward (blocking). `--socket`,
  `--local-port`.
* `m3dash open [ROOTS...]` — on this machine: serve on a loopback TCP
  port and open a browser (blocking). `--port`,
  `--open-browser`/`--no-open-browser`.
* `m3dash ls [ROOTS...] [--json]` — list discovered runs on the
  terminal.

Roots are fixed for a server's life; restart `socket`/`open` to change
them.

## The per-run dashboard

Clicking a run opens its dashboard, a single page top to bottom:

* a **crash banner** (only on failure) naming the likely-responsible
  component(s) and any collateral crashes;
* a **simulation graph** of the coupling (from `configuration.ymmsl` via
  the optional `graph` extra), with a colour legend and components coloured
  by status (running / finished / crashed) and the likely-responsible
  component on a crash outlined and its log opened automatically; click any
  component to inspect it (without the `graph` extra a dropdown lists the
  components instead);
* a **component summary** for the clicked component — a port block plus
  its program, settings and description, with referenced text files as
  inline links that open in a read-only viewer (with copy-path and
  copy-contents buttons);
* the **log files** — the manager log and each component's stdout/stderr,
  with an instance selector for multiplicity (vector-port) components.
