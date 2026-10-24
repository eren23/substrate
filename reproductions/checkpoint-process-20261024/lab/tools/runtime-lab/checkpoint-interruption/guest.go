package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"runtime"
	"time"
)

func main() {
	buf := make([]byte, 64<<20)
	if _, err := rand.Read(buf); err != nil {
		panic(err)
	}
	hash := sha256.Sum256(buf)
	boot := hex.EncodeToString(hash[:])
	if len(os.Args) > 1 && os.Args[1] == "seed" {
		if err := os.WriteFile("/root-witness", []byte(boot), 0600); err != nil {
			panic(err)
		}
		if err := os.WriteFile("/data/volume-witness", []byte(boot), 0600); err != nil {
			panic(err)
		}
	}
	count := 0
	last := ""
	for {
		b, err := os.ReadFile("/control/request")
		if err == nil && string(b) != last {
			last = string(b)
			count++
			h := sha256.Sum256(buf)
			root, _ := os.ReadFile("/root-witness")
			volume, _ := os.ReadFile("/data/volume-witness")
			out, _ := json.Marshal(map[string]any{"request": last, "count": count, "boot": boot, "memory_sha256": hex.EncodeToString(h[:]), "root_witness": string(root), "volume_witness": string(volume)})
			if err := os.WriteFile("/control/response.tmp", out, 0600); err != nil {
				panic(err)
			}
			if err := os.Rename("/control/response.tmp", "/control/response"); err != nil {
				panic(err)
			}
		}
		runtime.KeepAlive(buf)
		time.Sleep(20 * time.Millisecond)
	}
}
