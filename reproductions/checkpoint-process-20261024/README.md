# Checkpoint retry after an atelet process restart

This reproduces two deferred-checkpoint retry failures on Substrate
`1756c0f7cb344035889da699981eeff4c8aef755`. It contains the runnable fixture,
source/runtime pins and this results summary. Full test logs remain private.

The controller SIGKILLs its atelet test process, starts a fresh service/tracker
with production `Tracker.Start` over the same directories, and retries the same
request. A persistent worker runs the unmodified gVisor checkpoint/replay/fallback.

| Interruption, three trials each | Observed result |
| --- | --- |
| None; repeat the completed request | Passed 3/3 |
| Worker complete, before file moves | Passed 3/3 after restart |
| `CommitLocal` persisted, before cleanup/response | Passed 3/3 after restart |
| First image renamed into `.staged` | Failed 3/3; retry emptied the observed snapshot-file directory |
| Local publication, before `CommitLocal` | Failed 3/3; images/manifest retained, tracker state uncommitted |

Each failed retry attempted another real capture. Representative error:

```text
while checkpointing the sandbox: while running `runsc checkpoint`: exit status 128
FetchSpec failed: loading container: file does not exist
```

All nine controls restored the same 64 MiB memory witness after one capture,
with the counter advancing from 2 to 3. All five baseline packages passed
1,392 tests without failures, skips or race warnings. Cleanup and independent
evidence review passed. These are reported observations; this minimal packet
does not include the raw records needed to independently re-audit that run.

Scope: same-boot atelet test-process interruption, directly launched runsc guest,
MEMORY fidelity, systrap, network none, cgroups ignored, simulated storage/control
plane. This does not test full activation, worker/host restart or delayed upload.
Retained files in failed cases were not restore-tested.

## Reproduce

Use a disposable DigitalOcean Linux amd64 host with root/mount privileges,
Python 3.9+, Go 1.27.3, a C compiler, Git, procps, util-linux and 20 GiB free.
The tested runner retains its DigitalOcean metadata guard. It does not provision
or delete the VM; arrange its lifetime separately. Keep heavy builds off the Mac.

Clone this reproduction branch for the packet and use a separate clean upstream
checkout at the pin above. Extract the gVisor archive pinned in `manifest.json`.
Set the absolute paths below; output must be fresh below `/root/ax-validation`.

```sh
(
set -eu
packet=/absolute/path/to/reproductions/checkpoint-process-20261024
source=/absolute/path/to/clean-pinned-substrate
runtime=/absolute/path/to/extracted-directory-containing-runsc
test "$(uname -s)" = Linux
test "$(id -u)" = 0
test "$(GOTOOLCHAIN=local go env GOVERSION)" = go1.27.3
printf '%s  %s\n' 56996c546dbafd5716e1d09e307a131d97eb3b6483c22a1178631751f3e0392f "$runtime/runsc" | sha256sum -c -
mkdir -p /root/ax-validation
node_id=$(curl -fsS http://169.254.169.254/metadata/v1/id)
python3 -B "$packet/lab/tools/runtime-lab/checkpoint-process/run.py" \
  --source "$source" --runtime "$runtime" \
  --out /root/ax-validation/checkpoint-process-reproduction \
  --droplet-id "$node_id" --lab-commit "$(git -C "$packet" rev-parse HEAD)"
)
```

The runner checks source hashes, runs the five baseline packages with the tested
root/non-root split, compiles the process fixtures with `-race`, and runs 15 trials.
Expected exit on the pin: 1 (`findings`); 0 means all recovery invariants passed,
2 means inconclusive. Failed controls, timeout or cleanup stop the matrix.

The 14 source copies listed in `manifest.json` are byte-identical to the tested
commit. Two helper modules retain unchanged consumed functions; metadata drops
unused fixture entries. Packaging was checked offline without another live run.

Related report: [#2836](https://github.com/agent-substrate/substrate/issues/2836).
[PR #1139](https://github.com/agent-substrate/substrate/pull/1139) has related ideas
but uses older interfaces and has not been tested as a fix for this fixture.

AI-assisted investigation, fixtures, packaging and verification.
