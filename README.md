# SIA (Self-Improving AI) — Rust Port (in progress)

> **Status:** Active Rust port based on the original Python implementation. PR #10 landed a strong TDD foundation (orchestrator, web UI, Python subprocess bridge for target agents, config/profiles/providers). The next major milestone is implementing real LLM calling for Meta/Feedback agents (#35).

[![arXiv](https://img.shields.io/badge/arXiv-2605.27276-b31b1b.svg)](https://arxiv.org/abs/2605.27276)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

Official Rust implementation (with Python bridge) of [**SIA: Self Improving AI with Harness & Weight Updates**](https://arxiv.org/abs/2605.27276).

**Key architectural decision for Phase 1:** Target agents run via Python subprocess (with optional Docker sandbox). Meta and Feedback agents will use native Rust LLM clients. This gives us safety + performance wins while preserving the exact self-improvement semantics from the paper.

See [docs/RUST_PORT.md](docs/RUST_PORT.md) (added in PR #10) for current architecture and the integration boundary.

---

## Quick Links

- **Current high-priority work:** [Issue #35](https://github.com/micahstubbs/sia_rust/issues/35) — Multi-Provider LLM Client
- **Tracking:** [Umbrella Issue #34](https://github.com/micahstubbs/sia_rust/issues/34)
- **Open PR:** [#10](https://github.com/micahstubbs/sia_rust/pull/10) — Rust scaffold

## Original Python Documentation (still relevant for task format & evaluate.py contract)

The sections below are from the original Python README and remain useful for understanding task layout, the `evaluate.py` contract, and how to bring your own task.

---

## Run SIA locally with built-in tasks (Python bridge)

... (rest of original content follows — the core task running semantics are unchanged) ...