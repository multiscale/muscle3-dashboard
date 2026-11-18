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
from muscle3_dashboard.recorder.actor import run_recorder_actor


def deserialize(port: str, data: bytes) -> "MyMessage":
    # might have different deserialization behavior for different ports
    ...  # bytes -> your own message type


def main() -> None:
    run_recorder_actor(deserializer_for_port=deserialize)

if __name__ == '__main__':
  main()
```

Wire the recorder's `f_init` ports as extra receivers on the conduits you
want to record:

```yaml
components:
  rec:
    implementation: my_recorder
    ports:
      f_init: state_in
conduits:
  model.state_out: [consumer.state_in, rec.state_in]
settings:
  rec.config: /path/to/config.py
```

Each `reuse_instance()` iteration delivers one message per port, and the
recorder stops by itself once the senders close their ports. All ports of
one recorder advance together, so they must receive the same number of
messages; record senders running at different rates with one recorder each.

`run_recorder_actor` writes one live-tailable Zarr store per port per
outer-loop iteration, snapshots `config` next to the data for provenance,
and supports checkpoint/resume. Its settings are `config`, `store_path`,
`automatic_extract`, `automatic_extract_fields` and `record_interval` (only
record a message once that much simulation time has passed since the last
recorded one; the first and last message of each occurrence are always
kept). See `imas_muscle3.actors.recorder_component` for a complete
reference implementation.

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
