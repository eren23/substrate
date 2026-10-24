# Deferred checkpoint retry reproduction

The packet reproduces two retry failures after atelet starts moving a completed worker capture but before `CommitLocal`. It contains the exact Go fixture and instrumentation used in the corrected remote run on Substrate `1756c0f7cb344035889da699981eeff4c8aef755`. It does not contain a production fix.

The fixture keeps the production atelet checkpoint path and completion-record helpers. Its synthetic worker writes two images once, then allows replay only when production `ReuseCheckpoint` accepts the completion record. A one-shot error interrupts the chosen publication point. The test reconstructs tracker state within the same process and retries the same snapshot. It does not run `Tracker.Start`, background workers, or the control-plane workflow, and retains the same Atelet, worker and object store. The fixture returns `FailedPrecondition` instead of running the real worker's fallback when replay is unavailable. Real gVisor's fallback clears the checkpoint directory before attempting capture; unchanged fixture hashes do not establish production file preservation.

The first-stage and local-publication cases each failed three times, while durable publication and both uninterrupted controls passed. All five baseline packages passed with no skips. Separate real gVisor MEMORY replay/restore passed three times, preserving a 64 MiB random-buffer hash and continuing the guest counter. The failing fixture retries preserved every recorded file hash; no production data-loss, data-preservation, or process-crash result is claimed. The `committed` JSON field is derived from the returned response, not an independent tracker-state query. File-hash equality was checked in the evidence audit; the Go test logs those hashes but does not assert equality itself.

Use an isolated checkout at the pinned revision on a Linux test host with Python 3.9 or later, Go 1.27.3, a C compiler for the race detector, and access to Go module downloads. Run as an ordinary user with umask 022. This reproduction does not require Kubernetes or gVisor. The broader validation's worker-package tests require root; this command only runs the atelet fixture.

Set `packet`, `source`, and `overlay` to absolute paths. The overlay directory must not exist and must be outside the source checkout. From this packet directory:

```sh
(
set -eu
test "$(uname -s)" = Linux
test "$(id -u)" -ne 0
test "$(GOTOOLCHAIN=local go env GOVERSION)" = go1.27.3
packet=/absolute/path/to/substrate-checkpoint-retry-20261024
source=/absolute/path/to/substrate
overlay=/tmp/checkpoint-retry-overlay
python3 "$packet/prepare.py" --source "$source" --out "$overlay"
cd "$source"
umask 022
GOTOOLCHAIN=local GOWORK=off go test -json -race -count=1 -timeout=6m \
  -overlay "$overlay/overlay.json" \
  -run '^TestCheckpointBoundaryAudit$' ./cmd/atelet
)
```

The expected exit status on the tested revision is 1: six failing subtests across the two interruption points, with nine passing boundary/control subtests. Each case emits `AXLAB_AUDIT` JSON. The test itself repeats each case three times. To select only the two failing groups, use `-run '^TestCheckpointBoundaryAudit$/(first-stage|local-publication)$'`; retain the full command above to check controls.

`prepare.py` checks the clean source revision, relevant source hashes, fixture hash, and generated main-file hash before writing the overlay. It removes its newly created output directory if preparation raises an exception; an existing output is never removed. Keep the packet directory in place while running the test, since the overlay references its fixture. Both Go inputs are byte-identical to the remotely executed inputs. Standalone packaging was checked locally without recompiling Go. `evidence.json` contains the observed results and file hashes; `manifest.json` records provenance. The payload contains no cloud credentials or private lab infrastructure.

This reproducer accompanies the follow-up on [issue 2836](https://github.com/agent-substrate/substrate/issues/2836). It exercises worker replay from [PR 3015](https://github.com/agent-substrate/substrate/pull/3015) and staging from [PR 3048](https://github.com/agent-substrate/substrate/pull/3048). The older [PR 1139](https://github.com/agent-substrate/substrate/pull/1139) proposes destination recovery and interrupted-move handling, but has not been tested against this fixture.

Use a separate clean checkout at the pinned upstream revision for `source`; this branch adds the reproduction packet, so its HEAD is not the required source revision.

AI-assisted investigation and fixture preparation.
