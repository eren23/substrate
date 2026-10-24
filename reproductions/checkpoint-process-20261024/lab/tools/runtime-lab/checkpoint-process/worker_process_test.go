// Copyright 2026 Google LLC
// SPDX-License-Identifier: Apache-2.0

package main

import (
	"context"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"sync"
	"testing"

	"go.ate.dev/substrate/internal/actorlog"
	"go.ate.dev/substrate/internal/ateinterceptors"
	"go.ate.dev/substrate/internal/atunnel"
	"go.ate.dev/substrate/internal/atunnel/egress"
	"go.ate.dev/substrate/internal/atunnel/ingress"
	"go.ate.dev/substrate/internal/proto/ateworkerpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/status"
)

func TestProcessWorker(t *testing.T) {
	cfg := cpLoad(t)
	ctx := cpContext(t)
	var mu sync.Mutex
	count := 0
	record := func(ctx context.Context, req any, info *grpc.UnaryServerInfo, next grpc.UnaryHandler) (any, error) {
		result, err := next(ctx, req)
		mu.Lock()
		defer mu.Unlock()
		count++
		f, openErr := os.OpenFile(filepath.Join(cfg.Root, "worker-calls.jsonl"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
		if openErr != nil {
			panic(openErr)
		}
		message := ""
		if err != nil {
			message = err.Error()
		}
		entry := map[string]any{"call": count, "method": info.FullMethod, "request": req, "response": result, "code": status.Code(err).String(), "error": message, "pid": os.Getpid(), "token": cfg.Token}
		if writeErr := json.NewEncoder(f).Encode(entry); writeErr != nil {
			panic(writeErr)
		}
		if closeErr := f.Close(); closeErr != nil {
			panic(closeErr)
		}
		return result, err
	}
	listener := cpListen(t, cfg.WorkerSocket)
	srv := grpc.NewServer(grpc.ChainUnaryInterceptor(record, ateinterceptors.InternalServerUnaryInterceptor))
	service := NewService(&atunnel.Tunnel{Ingress: &ingress.Server{}, Egress: &egress.Proxy{}},
		actorlog.NewActorLogger(io.Discard, false), 64,
		&flagStore{cfg: &flagConfig{GlobalFlags: []string{"--ignore-cgroups", "--platform=systrap", "--network=none"}}})
	ateworkerpb.RegisterWorkerServer(srv, service)
	cpEmit(cfg, "ready", map[string]any{"role": "worker"})
	cpServe(t, ctx, srv, listener)
}
