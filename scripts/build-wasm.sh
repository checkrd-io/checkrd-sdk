#!/usr/bin/env bash
set -euo pipefail

echo "Building WASM core..."
cargo build --manifest-path crates/core/Cargo.toml --target wasm32-wasip1 --release

echo "Built: target/wasm32-wasip1/release/checkrd_core.wasm"
ls -lh target/wasm32-wasip1/release/checkrd_core.wasm
