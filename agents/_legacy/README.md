# agents/_legacy/

These files are the embedded Python agent implementations replaced by the Pi + Hermes agent services (Phase 12.5+).

| File | Replaced by |
|---|---|
| `enrichment.py` | Pi agent service — `agent_type=enrichment` |
| `osint.py` | Hermes agent service — `agent_type=osint` |
| `quality_digest.py` | Pi agent service — `agent_type=quality_digest` |
| `research.py` | Hermes agent service — `agent_type=research` |

These files are retained for reference and as a fallback when `agent_service_backend: python` is set in `config.yaml`.
Imports are via `agents._legacy.xxx` in pipeline.py when the python backend path is exercised.
