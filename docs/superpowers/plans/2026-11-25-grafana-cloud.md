# Grafana Cloud Implementation Plan

> Required for implementers. Use test-driven development and follow the repository's AGENTS.md.

The service uses the existing Grafana OTLP gateway with an official protobuf transport, stable service.name, and the structured data already used by operations. See docs/superpowers/specs/2026-11-25-grafana-cloud-design.md.

## Task 1

- [ ] Add or update a failing focused test that exercises the production telemetry path and checks the new OTLP signal and structured payload.
- [ ] Switch both jobs observability entrypoints and applicable Actions workflows to Grafana OTLP logs. Preserve batches, per-run records, flush and hard-exit semantics. Remove Better Stack heartbeat dispatches after verifying run completion/failure has a Grafana record where replacement is possible, without changing scraper execution. Ensure workflows receive OTEL_* via scoped secrets and Docker image dependencies remain available. Run repository tests.
- [ ] Cover missing configuration and failed or timed-out exports and keep normal app and workflow execution independent from telemetry availability.
- [ ] Update relevant environment templates and current deploy guidance. Leave historical docs alone.
- [ ] Verify checks and commit the scoped change. The orchestrator handles PR, squash merge, deployment and live checks.
