# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-06-05

### Added
- WeChat 4.0 memory-based key extraction (AES-256-CBC + HMAC-SHA512)
- Full database decryption (26 databases: session, message, contact, etc.)
- Real-time message monitoring via WAL polling (~100ms latency)
- Web UI with SSE push (localhost:5678)
- MCP Server for Claude AI integration (5 tools: sessions, chat history, search, contacts, new messages)
- Image decryption for V2 format (AES-128-ECB + XOR)
- XOR / V1 / V2 multi-format image decryption support
- latency_test.py diagnostic tool

### Technical Highlights
- SQLCipher 4 support with page size 4096 + reserve 80
- WAL frame salt validation to skip stale frames
- 30ms mtime polling + WAL patch for real-time monitoring
- Process memory scan with HMAC verification for key extraction
