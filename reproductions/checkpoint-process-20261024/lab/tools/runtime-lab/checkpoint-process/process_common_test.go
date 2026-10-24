// Copyright 2026 Google LLC
// SPDX-License-Identifier: Apache-2.0

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"
	"testing"

	"go.ate.dev/substrate/internal/nodepath"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// These helpers are compiled only into private experiment test binaries.
type cpConfig struct {
	Root           string `json:"root"`
	Token          string `json:"token"`
	ActorUID       string `json:"actor_uid"`
	Request        string `json:"request"`
	WorkerSocket   string `json:"worker_socket"`
	AteletSocket   string `json:"atelet_socket"`
	ServicesSocket string `json:"services_socket"`
	AssetSHA256    string `json:"asset_sha256"`
}

var cpOutput sync.Mutex

func cpLoad(t *testing.T) cpConfig {
	t.Helper()
	var cfg cpConfig
	data, err := os.ReadFile(os.Getenv("CP_CONFIG"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &cfg); err != nil {
		t.Fatal(err)
	}
	if cfg.Token == "" || cfg.ActorUID == "" || !filepath.IsAbs(cfg.Root) {
		t.Fatal("incomplete process configuration")
	}
	nodepath.ActorsDir = filepath.Join(cfg.Root, "actors")
	nodepath.SnapshotsDir = filepath.Join(cfg.Root, "snapshots")
	return cfg
}

func cpEmit(cfg cpConfig, event string, fields map[string]any) {
	cpOutput.Lock()
	defer cpOutput.Unlock()
	fields["event"], fields["pid"], fields["token"] = event, os.Getpid(), cfg.Token
	data, err := json.Marshal(fields)
	if err != nil {
		panic(err)
	}
	fmt.Printf("CP_EVENT %s\n", data)
}

func cpContext(t *testing.T) context.Context {
	t.Helper()
	ctx, stop := signal.NotifyContext(t.Context(), syscall.SIGTERM, syscall.SIGINT)
	t.Cleanup(stop)
	return ctx
}

func cpDial(t *testing.T, socket string) *grpc.ClientConn {
	t.Helper()
	conn, err := grpc.NewClient("unix://"+socket, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}

func cpListen(t *testing.T, socket string) net.Listener {
	t.Helper()
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	return listener
}

func cpServe(t *testing.T, ctx context.Context, srv *grpc.Server, listener net.Listener) {
	t.Helper()
	done := make(chan error, 1)
	go func() { done <- srv.Serve(listener) }()
	t.Cleanup(srv.Stop)
	select {
	case <-ctx.Done():
		srv.Stop()
		<-done
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	}
}
