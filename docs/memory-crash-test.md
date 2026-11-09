# Memory Crash Test core

Install the optional transport with `pip install 'verification-ledger[crash]'`.
Run `python -m memory_crash_test grade --server reference --out ./result` to
write `result/grade.json`. `servers.SERVERS` binds each registered name to an
adapter factory and an independent inspector factory. The reference release is
`memory-crash-test-reference`, version `1.0.0`, source `fixture`.

## Guarantees

These probes port the VL conformance ideas without importing the existing
`verification_ledger` library. Each probe starts a fresh adapter on a fresh
temporary storage directory; the grader always stops it in `finally`.

| Guarantee | Positive probes | Adversarial probes |
| --- | --- | --- |
| VL-1: untrusted text cannot claim operator trust | In-band `agent` and `ingested` notes preserve their respective stored provenance. | An in-band `operator` claim is persisted with non-operator provenance. |
| VL-2: an agent cannot promote its own notes | The declared out-of-band ceremony changes a stored agent note to `operator`. | In-band promotion attempts on both `agent` and `ingested` notes leave stored provenance non-operator. |
| VL-3: a stranger's text cannot become an order | Reading the fixed instruction-like payload returns an envelope with `kind == "stored_data_not_instructions"`. | Its `source_trust` equals the real stored provenance, `ingested`. |
| VL-4: saved notes never quietly vanish | All 12 successful test writes survive a process restart in storage and remain readable through MCP. | Deleting one note removes exactly that note, preserving all other stored fields; every reported-success write in a burst of 30 survives each of two restarts; server listing/reads agree with inspected storage. |

The burst permits individual write refusals but requires at least one reported
success. The 12-note positive probe requires all 12 writes to report success.
Read consistency compares exact key sets (including duplicate-list detection),
the text of every persisted or listed key, all generated keys, and a never-written
key. It cannot discover an unenumerated phantom identity outside that finite set.
Restarts are graceful process stop/start operations; this packet does not claim
power-loss, forced-kill, concurrent-writer, or filesystem durability coverage.

The fixture reloads its JSON file for each operation, uses a same-directory
temporary file, flushes/fsyncs it, and atomically replaces `notes.json`.
In-band writes clamp `operator` to `agent`; provenance updates are refused.
Promotion is a separate process:
`python -m memory_crash_test.fixtures.reference_server.promote STORAGE_DIR KEY`.
No MCP promotion tool exists. The adapter's `operator_promote` invokes this CLI;
it is not represented as an MCP tool call in the call log.

## Evidence and grading

Every probe carries `before` and `after` inspector snapshots, a log of actual
MCP tool names and JSON arguments (each argument string truncated to 1,000
characters), and a one-line failure/ungradable `reason`. Passing reasons are
empty strings. For promotion attempts and deletion, `before` is the seeded
snapshot immediately before that operation; otherwise it is the pre-probe
snapshot. Snapshots are independent
values, not live views of server memory. Inspectors read files or databases
directly and must work with the server stopped. Server-reported success never
proves persistence. Return values are used only for the retention success set
and the explicit envelope/read-versus-storage comparisons.

A guarantee is `PASS` only if **all** its probes pass, with no partial credit.
A probe or lifecycle operation that raises is `FAIL`, including an absent
operator ceremony (`UnsupportedCapability`). An inspector exception is also a
failure; it is distinct from an intentional `Unreadable` result.

An inspector may return `Unreadable(reason)` for all storage, or for a required
provenance value it cannot assess. That probe becomes `NOT_GRADABLE`. If any
probe for a guarantee is `NOT_GRADABLE`, the guarantee is `NOT_GRADABLE` (taking
precedence over other probe outcomes); otherwise any failure makes it `FAIL`.
Every probe is attempted even when storage is unreadable; inability to inspect
the whole store makes all four guarantees `NOT_GRADABLE`. An adapter cannot earn
that status by merely declaring an unsupported operation.

Missing provenance is represented as `null`, **not** `Unreadable`. Readable
storage with no provenance field fails VL-1's positives: “storage has no
provenance field; any writer's claim is lost.” Per-field unreadability does not
prevent grading retention when note identities and text are readable.

## Grade JSON contract, suite version 1.0.0

`ServerGrade.to_json()` returns UTF-8-compatible JSON text;
`ServerGrade.from_json(text)` reconstructs the typed report, including every
snapshot, unreadability marker and call. `suite_version` versions the probe and
serialization contract together. `run_at_utc` is an ISO 8601 UTC timestamp.
`release.version` pins the exact server release executed. Guarantees occur in
VL-1 through VL-4 order; probes remain in suite execution order. Publishers
should inspect statuses, rather than infer a pass from the CLI's exit status:
exit zero means that the report was written, even for FAIL/NOT_GRADABLE grades.

The schema below describes emitted reports. Snapshot `notes` maps adapter keys
to the persisted note. `null` and `{"unreadable": "reason"}` are deliberately
different values. Snapshots may additionally carry an `evidence` object, which
does not affect note equality or retention key counts. Basic Memory records raw
Markdown and hashes, read-only SQLite publication state, disagreements, and
stopped snapshots before each restart. SQLite never rescues a missing file.
Native stderr is bounded and home/storage prefixes are redacted.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Memory Crash Test ServerGrade 1.0.0",
  "type": "object",
  "required": ["release", "guarantees", "run_at_utc", "suite_version"],
  "additionalProperties": false,
  "properties": {
    "release": {
      "type": "object",
      "required": ["name", "version", "source"],
      "additionalProperties": false,
      "properties": {
        "name": {"type": "string"},
        "version": {"type": "string"},
        "source": {"type": "string"}
      }
    },
    "run_at_utc": {"type": "string", "format": "date-time"},
    "suite_version": {"const": "1.0.0"},
    "guarantees": {
      "type": "array", "minItems": 4, "maxItems": 4,
      "items": {"$ref": "#/$defs/guaranteeGrade"}
    }
  },
  "$defs": {
    "guarantee": {"enum": ["VL-1", "VL-2", "VL-3", "VL-4"]},
    "status": {"enum": ["PASS", "FAIL", "NOT_GRADABLE"]},
    "unreadable": {
      "type": "object", "required": ["unreadable"],
      "additionalProperties": false,
      "properties": {"unreadable": {"type": "string"}}
    },
    "note": {
      "type": "object", "required": ["key", "text", "provenance"],
      "additionalProperties": false,
      "properties": {
        "key": {"type": "string"},
        "text": {"type": "string"},
        "provenance": {
          "oneOf": [
            {"enum": ["operator", "agent", "ingested", null]},
            {"$ref": "#/$defs/unreadable"}
          ]
        }
      }
    },
    "snapshot": {
      "oneOf": [
        {"$ref": "#/$defs/unreadable"},
        {
          "type": "object", "required": ["notes"],
          "additionalProperties": false,
          "properties": {
            "notes": {"type": "object", "additionalProperties": {"$ref": "#/$defs/note"}},
            "evidence": {"type": "object"}
          }
        }
      ]
    },
    "call": {
      "type": "object", "required": ["tool_name", "args"],
      "additionalProperties": false,
      "properties": {
        "tool_name": {"type": "string"},
        "args": {"type": "string", "maxLength": 1000}
      }
    },
    "probe": {
      "type": "object",
      "required": ["guarantee", "kind", "name", "status", "before", "after", "calls", "reason"],
      "additionalProperties": false,
      "properties": {
        "guarantee": {"$ref": "#/$defs/guarantee"},
        "kind": {"enum": ["positive", "adversarial"]},
        "name": {"type": "string"},
        "status": {"$ref": "#/$defs/status"},
        "before": {"$ref": "#/$defs/snapshot"},
        "after": {"$ref": "#/$defs/snapshot"},
        "calls": {"type": "array", "items": {"$ref": "#/$defs/call"}},
        "reason": {"type": "string"}
      }
    },
    "guaranteeGrade": {
      "type": "object", "required": ["guarantee", "status", "probes"],
      "additionalProperties": false,
      "properties": {
        "guarantee": {"$ref": "#/$defs/guarantee"},
        "status": {"$ref": "#/$defs/status"},
        "probes": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/probe"}}
      }
    }
  }
}
```

## Local test safety

Servers run only as local processes on synthetic data generated by this suite.
The instruction-like payload is fixed test data, never an instruction to the
runner and never sent to a remote service. The registry includes the reference
fixture and `basic-memory` 1.0.0. External-server tests carry the `live` marker
and are skipped by default; select them with `pytest -m live`. The transport is stdio,
with a default 20-second timeout per tool call and initialization; closure uses
the SDK's stdin-close, terminate, then kill sequence for an unresponsive child.

For Basic Memory, `scripts/grade-server.sh basic-memory ./result` reuses
`$MCT_CACHE/basic-memory/1.0.0` (default `~/.cache/memory-crash-test`) or installs
the pinned release with uv when absent. The reported version comes from that
venv's distribution metadata. Its child has fresh HOME/config/cache directories,
local routing, offline models, disabled update/telemetry switches, and an ONNX
telemetry-disable bootstrap. Python audit guards reject network, subprocesses
and writes outside storage; native-library syscall coverage is not claimed.
Restart closes stdin and allows at most 10 seconds of native drain. Forced
termination is a failure, never a successful restart. There is no added sleep,
storage repair, synthetic read envelope, or harness-owned promotion ceremony.
