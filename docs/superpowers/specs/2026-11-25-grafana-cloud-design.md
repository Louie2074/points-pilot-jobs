# Route Points Pilot telemetry to Grafana Cloud

Use the user's existing Grafana Cloud OpenTelemetry gateway for Points Pilot operational telemetry. Preserve structured timing and run event fields as OTLP logs, so Loki can query their JSON payloads. Keep meaningful service names and failure isolation. Preserve independent PostHog product and AI analytics. Use standard OTEL_* settings with an HTTP/protobuf exporter. Auth headers are supplied only by deployed secret managers. Remove the old Better Stack ingest paths from the service rather than deleting the historical account data.

The production rollout requires a deployment or workflow run and a gateway ingestion check before the old configuration is retired.
