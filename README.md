# kvcomplus-sila2

A **SiLA 2 server** that exposes KEYENCE PLC device access over the KV COM+
Library (KV-DH1L), wrapping its `DBCommManager` ActiveX/COM component via
[pywin32](https://github.com/mhammond/pywin32). The server is stateless: each
command opens a KV COM+ connection, operates, and disconnects.

The wrapper is intentionally **signal-agnostic** — it forwards the library's own
`ReadDevice` / `WriteDevice` (one word at a time) and reports the PLC run mode.
It assigns no meaning to particular device addresses; the meaning of signals
(travel carriage, hand, etc.) lives in the client.

> ⚠️ **Disclaimer**: This software is under active development. It comes with
> **no warranty** and the authors accept **no liability** for any results or
> damages. Use at your own risk, and verify safety before connecting to real
> hardware.

## Features (v1)

- **DeviceService**: `ReadDevice`, `WriteDevice` (single word)
- **ConnectionService**: `GetMode` (RUN / PROGRAM; also confirms reachability)

Multi-word (e.g. 32-bit) values are reconstructed client-side from consecutive
single-word reads/writes (low word at the address, high word at address + 1,
signed int32), matching the existing `kv-scripts` behaviour.

## Requirements

Windows with the **KV COM+ Library (KV-DH1L)** installed and COM-registered,
Python >= 3.11, and [uv](https://docs.astral.sh/uv/). `pywin32` is installed
automatically on Windows.

## Setup

```bash
git clone git@github.com:kaizu/kvcomplus-sila2.git
cd kvcomplus-sila2
uv sync
```

## Configuration

Connection settings are given in a TOML file (not as command arguments). Copy
and edit the example:

```bash
cp config.example.toml config.toml
```

See [config.example.toml](config.example.toml) for all keys (`[plc]`
peer / plc_id / progid, `[server]` host / port).

## Run the server

```bash
uv run python -m kvcomplus_sila2 --config config.toml --insecure
```

`--insecure` disables encryption. Listen address/port come from `[server]`;
override with `-a/--ip-address` and `-p/--port`. See `--help` for TLS options.

## Run the sample client

With the server running, in another terminal:

```bash
uv run python samples/client_example.py --host 127.0.0.1 --port 50052
```

## License

[MIT](LICENSE) © 2026 Kazunari Kaizu
