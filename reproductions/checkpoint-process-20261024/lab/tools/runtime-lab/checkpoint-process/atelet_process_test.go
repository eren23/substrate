// Copyright 2026 Google LLC
// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bufio"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.ate.dev/substrate/api/ateapi/v1"
	objectstorev1 "go.ate.dev/substrate/api/objectstore/v1"
	"go.ate.dev/substrate/cmd/atelet/internal/actordirs"
	"go.ate.dev/substrate/cmd/atelet/internal/ateletpath"
	"go.ate.dev/substrate/cmd/atelet/internal/snapshottracker"
	"go.ate.dev/substrate/internal/ateinterceptors"
	"go.ate.dev/substrate/internal/objectstoreplugin"
	"go.ate.dev/substrate/internal/proto/ateletpb"
	"go.ate.dev/substrate/pkg/objectstorage"
	"google.golang.org/grpc"
	"google.golang.org/grpc/status"
	"k8s.io/utils/clock"
)

var cpAteletConfig cpConfig

// The overlay calls this after the exact operation under observation.
// It never returns at the selected boundary: the controller must SIGKILL us.
func cpBoundary(point string) {
	if point != os.Getenv("CP_FAULT") {
		return
	}
	cpEmit(cpAteletConfig, "boundary", map[string]any{"point": point, "actor_uid": cpAteletConfig.ActorUID})
	select {}
}

func cpRequest(t *testing.T, cfg cpConfig) *ateletpb.CheckpointRequest {
	t.Helper()
	data, err := os.ReadFile(cfg.Request)
	if err != nil {
		t.Fatal(err)
	}
	req := &ateletpb.CheckpointRequest{}
	if err := json.Unmarshal(data, req); err != nil {
		t.Fatal(err)
	}
	if req.GetActorUid() != cfg.ActorUID {
		t.Fatal("request actor mismatch")
	}
	return req
}

func TestProcessInitialize(t *testing.T) {
	cfg := cpLoad(t)
	if _, err := os.Stat(filepath.Join(cfg.Root, "initialized")); !os.IsNotExist(err) {
		t.Fatal("initialization is one-shot", err)
	}
	if err := actordirs.ResetActorDirs(cfg.ActorUID); err != nil {
		t.Fatal(err)
	}
	rec := &sandboxAssetsRecord{SandboxClass: "gvisor", Assets: map[string]assetEntry{
		gvisorAssetName: {URL: "gs://fixture-only/observed-runtime.tar.zstd", SHA256: cfg.AssetSHA256},
	}}
	if err := writeSandboxRecord(cfg.ActorUID, rec); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cfg.Root, "initialized"), []byte(cfg.Token), 0600); err != nil {
		t.Fatal(err)
	}
	cpEmit(cfg, "initialized", map[string]any{
		"actor_dirs": ateletpath.ActorDirs(cfg.ActorUID),
		"state_path": ateletpath.ActorSnapshotStateFile(cfg.ActorUID),
		"local_dir":  ateletpath.LocalSnapshotDir(cfg.ActorUID, cpRequest(t, cfg).GetSnapshot().GetSnapshotUuid()),
	})
}

type cpReportServer struct {
	ateapi.UnimplementedWorkerServiceServer
	cfg cpConfig
}

func (s *cpReportServer) ReportActorSnapshotState(_ context.Context, req *ateapi.ReportActorSnapshotStateRequest) (*ateapi.ReportActorSnapshotStateResponse, error) {
	f, err := os.OpenFile(filepath.Join(s.cfg.Root, "reports.jsonl"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	if err := json.NewEncoder(f).Encode(req); err != nil {
		return nil, err
	}
	return &ateapi.ReportActorSnapshotStateResponse{}, nil
}

func TestProcessServices(t *testing.T) {
	cfg := cpLoad(t)
	ctx := cpContext(t)
	plugin, err := objectstoreplugin.NewNodePlugin(&recordingObjectStorage{}, cfg.Root, objectstorage.DefaultUploadMemory)
	if err != nil {
		t.Fatal(err)
	}
	listener := cpListen(t, cfg.ServicesSocket)
	srv := grpc.NewServer(grpc.UnaryInterceptor(ateinterceptors.InternalServerUnaryInterceptor))
	objectstorev1.RegisterNodeProviderServer(srv, plugin)
	ateapi.RegisterWorkerServiceServer(srv, &cpReportServer{cfg: cfg})
	cpEmit(cfg, "ready", map[string]any{"role": "services"})
	cpServe(t, ctx, srv, listener)
}

func cpState(t *testing.T, cfg cpConfig, s *Atelet) map[string]any {
	t.Helper()
	var disk any
	data, err := os.ReadFile(ateletpath.ActorSnapshotStateFile(cfg.ActorUID))
	if err == nil {
		if err := json.Unmarshal(data, &disk); err != nil {
			t.Fatal(err)
		}
	} else if !os.IsNotExist(err) {
		t.Fatal(err)
	}
	return map[string]any{"tracker": s.snapshots.State(cfg.ActorUID), "disk": disk}
}

func TestProcessAtelet(t *testing.T) {
	cfg := cpLoad(t)
	cpAteletConfig = cfg
	ctx := cpContext(t)
	boot, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		t.Fatal(err)
	}
	bootID := strings.TrimSpace(string(boot))
	ateletpath.SnapshotScratchDir = filepath.Join(cfg.Root, "scratch")
	if err := os.MkdirAll(ateletpath.SnapshotScratchDir, 0700); err != nil {
		t.Fatal(err)
	}
	services := cpDial(t, cfg.ServicesSocket)
	workerSocketPath = func(string) string { return cfg.WorkerSocket }
	store := &trackerStore{}
	workers := ateapi.NewWorkerServiceClient(services)
	snapshots := snapshottracker.New(ctx, store, clock.RealClock{},
		snapshottracker.WithBootCheck(localSnapshotBootCheck(bootID)),
		snapshottracker.WithReporter(&trackerReporter{workers: workers}))
	s := NewService(ctx, newWorkerDialer(1), nil,
		objectstorev1.NewNodeProviderClient(services), nil, cfg.ServicesSocket,
		nil, newTestAssetCacheAt(t, filepath.Join(cfg.Root, "asset-cache")), nil,
		newRuntimeIndex(), nil, nil, nil, newSystemInfoVolumeRefresher(nil, nil),
		snapshots, workers, bootID)
	store.s = s
	if os.Getenv("CP_INITIAL") == "1" {
		end, err := snapshots.BeginStart(ctx, cfg.ActorUID)
		if err != nil {
			t.Fatal(err)
		}
		end()
	}
	snapshots.Start()
	listener := cpListen(t, cfg.AteletSocket)
	srv := grpc.NewServer(grpc.UnaryInterceptor(ateinterceptors.InternalServerUnaryInterceptor))
	ateletpb.RegisterAteletServer(srv, s)
	state := cpState(t, cfg, s)
	state["boot_id"], state["startup_complete"] = bootID, true
	cpEmit(cfg, "ready", state)
	go func() {
		scanner := bufio.NewScanner(os.Stdin)
		for scanner.Scan() {
			var command struct {
				Op string `json:"op"`
				ID string `json:"id"`
			}
			if err := json.Unmarshal(scanner.Bytes(), &command); err != nil {
				panic(err)
			}
			if command.Op != "state" {
				panic("unknown process command")
			}
			record := cpState(t, cfg, s)
			record["id"] = command.ID
			cpEmit(cfg, "state", record)
		}
	}()
	cpServe(t, ctx, srv, listener)
	waitCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := snapshots.Wait(waitCtx); err != nil {
		t.Error(err)
	}
}

func TestProcessClient(t *testing.T) {
	cfg := cpLoad(t)
	req := cpRequest(t, cfg)
	conn := cpDial(t, cfg.AteletSocket)
	ctx, cancel := context.WithTimeout(t.Context(), 60*time.Second)
	defer cancel()
	response, err := ateletpb.NewAteletClient(conn).Checkpoint(ctx, req)
	message := ""
	if err != nil {
		message = err.Error()
	}
	cpEmit(cfg, "response", map[string]any{"code": status.Code(err).String(), "error": message, "response": response})
}
