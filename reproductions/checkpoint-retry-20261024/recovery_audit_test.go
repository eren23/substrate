// Copyright 2026 Google LLC
// SPDX-License-Identifier: Apache-2.0

package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"slices"
	"sync"
	"testing"

	"go.ate.dev/substrate/cmd/atelet/internal/ateletpath"
	"go.ate.dev/substrate/cmd/atelet/internal/snapshottracker"
	"go.ate.dev/substrate/internal/ateworker"
	"go.ate.dev/substrate/internal/proto/ateletpb"
	"go.ate.dev/substrate/internal/proto/ateworkerpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"k8s.io/utils/clock"
)

// These hooks exist only in the private Go overlay, never in upstream source.
var auditFaultPoint string
var auditFaultReached bool
var errAuditInterrupted = errors.New("audit interruption before tracker commit")

func auditCheckpointBoundary(point string) error {
	if auditFaultPoint == point && !auditFaultReached {
		auditFaultReached = true
		return errAuditInterrupted
	}
	return nil
}

// The worker is a fixture: it writes synthetic images once and uses the merged
// production completion-record validator for every later RPC. It never reruns
// or simulates runsc. Real runtime capture is verified in a separate experiment.
type auditReplayWorker struct {
	*fakeWorker
	mu              sync.Mutex
	captures, calls int
}

func (w *auditReplayWorker) CheckpointWorkload(ctx context.Context, req *ateworkerpb.CheckpointWorkloadRequest) (*ateworkerpb.CheckpointWorkloadResponse, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.calls++
	dir := req.GetActorDirs().GetCheckpointDir()
	if w.captures != 0 {
		if response := ateworker.ReuseCheckpoint(dir, req.GetFidelity()); response != nil {
			return response, nil
		}
		return nil, status.Error(codes.FailedPrecondition, "fixture workload stopped and completed capture cannot be replayed")
	}
	response, err := w.fakeWorker.CheckpointWorkload(ctx, req)
	if err != nil {
		return nil, err
	}
	slices.Sort(response.SnapshotFiles)
	if err := ateworker.RecordCheckpoint(dir, req.GetFidelity(), response); err != nil {
		return nil, err
	}
	w.captures++
	return response, nil
}

func auditActor(t *testing.T) (*pluginActor, *auditReplayWorker) {
	t.Helper()
	a := runPluginActor(t)
	a.s.bootID = "audit-boot-1"
	a.worker.snapshotFiles = map[string]string{"checkpoint.img": "guest-memory", "pages.img": "guest-pages"}
	w := &auditReplayWorker{fakeWorker: a.worker}
	dir, err := os.MkdirTemp("", "audit-worker-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	sock := filepath.Join(dir, "worker.sock")
	lis, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	srv := grpc.NewServer()
	ateworkerpb.RegisterWorkerServer(srv, w)
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)
	previous := workerSocketPath
	workerSocketPath = func(string) string { return sock }
	t.Cleanup(func() { workerSocketPath = previous })
	// Drop the cached connection used by Run so Checkpoint reaches this fixture.
	a.s.workerDialer = newWorkerDialer(1)
	a.s.snapshots = snapshottracker.New(t.Context(), &trackerStore{s: a.s}, clock.RealClock{},
		snapshottracker.WithBootCheck(localSnapshotBootCheck(a.s.bootID)))
	return a, w
}

func auditFileHashes(t *testing.T, root string) map[string]string {
	t.Helper()
	result := map[string]string{}
	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !entry.Type().IsRegular() {
			return nil
		}
		b, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		hash := sha256.Sum256(b)
		result[rel] = hex.EncodeToString(hash[:])
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return result
}

func TestCheckpointBoundaryAudit(t *testing.T) {
	for _, point := range []string{"first-stage", "local-publication", "durable-publication", "deferred-control", "immediate-control"} {
		for iteration := 1; iteration <= 3; iteration++ {
			t.Run(fmt.Sprintf("%s/%d", point, iteration), func(t *testing.T) {
				a, worker := auditActor(t)
				mode := ateletpb.UploadMode_UPLOAD_MODE_DEFERRED
				if point == "durable-publication" || point == "immediate-control" {
					mode = ateletpb.UploadMode_UPLOAD_MODE_IMMEDIATE
				}
				fault := point != "deferred-control" && point != "immediate-control"
				auditFaultPoint, auditFaultReached = point, false
				t.Cleanup(func() { auditFaultPoint, auditFaultReached = "", false })
				snap := a.snapshot("audit-snapshot", 1)
				_, firstErr := a.checkpoint(t, snap, mode)
				if fault {
					if !auditFaultReached || !errors.Is(firstErr, errAuditInterrupted) {
						t.Fatalf("requested boundary not reached: %v", firstErr)
					}
				} else if firstErr != nil {
					t.Fatalf("uninterrupted capture failed: %v", firstErr)
				}
				worker.mu.Lock()
				firstCaptures := worker.captures
				worker.mu.Unlock()
				if firstCaptures != 1 {
					t.Fatalf("fixture capture count = %d", firstCaptures)
				}
				before := auditFileHashes(t, ateletpath.ActorSnapshotPath(pluginActorUID))
				auditFaultPoint = ""
				// Same-boot tracker reconstruction reads the actual on-disk state;
				// object storage and the stopped-worker fixture remain available.
				a.s.snapshots = snapshottracker.New(t.Context(), &trackerStore{s: a.s}, clock.RealClock{},
					snapshottracker.WithBootCheck(localSnapshotBootCheck(a.s.bootID)))
				response, retryErr := a.checkpoint(t, snap, mode)
				after := auditFileHashes(t, ateletpath.ActorSnapshotPath(pluginActorUID))
				state := response.GetState()
				committed := proto.Equal(state.GetLatestLocal().GetSnapshot(), snap) || proto.Equal(state.GetLatestDurable().GetSnapshot(), snap)
				payloadOK := true
				for name, expected := range a.worker.snapshotFiles {
					b, err := os.ReadFile(filepath.Join(ateletpath.LocalSnapshotDir(pluginActorUID, snap.GetSnapshotUuid()), name))
					if err != nil || string(b) != expected {
						payloadOK = false
					}
				}
				worker.mu.Lock()
				captures, calls := worker.captures, worker.calls
				worker.mu.Unlock()
				ok := retryErr == nil && committed && payloadOK && captures == 1
				outcome := "pass"
				if !ok {
					outcome = "finding"
				}
				errorText := ""
				if retryErr != nil {
					errorText = retryErr.Error()
				}
				b, err := json.Marshal(map[string]any{"test": t.Name(), "outcome": outcome, "details": map[string]any{
					"scope":         "synthetic worker images; production atelet and checkpoint record validator; same-boot tracker reconstruction",
					"fault_reached": auditFaultReached, "snapshot_uuid": snap.GetSnapshotUuid(), "generation": snap.GetGeneration(),
					"captures": captures, "worker_calls": calls, "retry_error": errorText, "committed": committed,
					"payload_readable": payloadOK, "before_sha256": before, "after_sha256": after,
				}})
				if err != nil {
					t.Fatal(err)
				}
				t.Log("AXLAB_AUDIT " + string(b))
				if !ok {
					t.Error("checkpoint retry invariant failed; see audit witness")
				}
			})
		}
	}
}
