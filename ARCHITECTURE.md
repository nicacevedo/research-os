# Research OS Architecture

Research OS is a local-first scientific orchestration system for reproducible,
evidence-grounded, auditable, and selectively autonomous research.

Its purpose is not to replace scientific judgment. Its purpose is to make
scientific exploration, literature synthesis, experimentation, validation, and
writing easier to reproduce, inspect, and coordinate.

## 1. Operating model

The default workflow is:

```text
local deterministic computation
        ↓
interesting / uncertain event
        ↓
high-quality scientific reasoning
        ↓
structured scientific state
        ↓
local validation / indexing / monitoring