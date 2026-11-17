# Panel dashboard for MUSCLE3 simulations
muscle_dashboard is a Panel based dashboard for log parsing and debugging of MUSCLE3 simulations.

# Installation
Quick developer installation guide

Requires **Python 3.11 or newer** (3.12+ recommended: current releases of
panel, bokeh, numpy and zarr need it). Check with `python3 --version` before
creating the venv, since the venv uses whichever Python creates it; if your
default `python3` is older, create it with e.g. `python3.12 -m venv ./venv`.

```bash
git clone git@github.com:multiscale/muscle3-dashboard.git
cd muscle3-dashboard
python3 -m venv ./venv
. venv/bin/activate
pip install -e .[dev]
pytest
```

The per-run **simulation graph** is drawn from the run's `configuration.ymmsl`
by [`ymmsl2svg`](https://github.com/multiscale/ymmsl2svg).

# How to use
```bash
# make sure your virtual environment is activated
muscle_dashboard path/to/my/muscle/simulation/workdir
```

# Recording your own data

> **The MUSCLE3 recorder actor is temporarily disabled.** It relied on
> MUSCLE3 internals that changed in muscle3 0.11; `run_recorder_actor()` now
> raises a clear error. muscle3-dashboard no longer depends on muscle3.

Recording does not need MUSCLE3: any Python code can write data that the
dashboard shows in a live/replay Recorder tab -- no dashboard-side
configuration needed, since a recording is recognised purely by its on-disk
footprint.

Bring two things: a `deserialize(data) -> your_message` function per port,
and a `config` file defining `extract(message) -> dict[str, xarray.Dataset]`
(or a `State` class -- see `muscle3_dashboard.visualization.base_state` --
so the same file can double as a `Plotter` for live viewing). Then feed it
messages: anything with `timestamp`, `next_timestamp` and `data` (a
`libmuscle.Message` works as-is, or use `Record`):

```python
from pathlib import Path

from muscle3_dashboard.recorder.base import Record
from muscle3_dashboard.recorder.collection import RecorderCollection
from muscle3_dashboard.recorder.zarr_recorder import ZarrRecorder

collection = RecorderCollection(
    Path("store"),
    Path("my_config.py"),
    {"state": deserialize},
    ZarrRecorder,
)
for i, payload in enumerate(payloads):
    last = i == len(payloads) - 1
    collection.handle("state", Record(float(i), None if last else i + 1.0, payload))
collection.close()
```

`RecorderCollection` writes one live-tailable Zarr store per port per
occurrence (a message with `next_timestamp=None` ends a stream; the next one
starts a new occurrence), snapshots `config` next to the data for
provenance, and supports checkpoint/resume via `collection.get_state()`/
`restore_state()`.

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
  `ymmsl2svg`), with a colour legend and components coloured
  by status (running / finished / crashed) and the likely-responsible
  component on a crash outlined and its log opened automatically; click any
  component to inspect it;
* a **component summary** for the clicked component — a port block plus
  its program, settings and description, with referenced text files as
  inline links that open in a read-only viewer (with copy-path and
  copy-contents buttons);
* the **log files** — the manager log and each component's stdout/stderr,
  with an instance selector for multiplicity (vector-port) components.
