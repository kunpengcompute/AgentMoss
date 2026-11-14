# Third-party source

`aio_backend.py` adapts the AIOSandboxBackend example from
[agent-infra/sandbox](https://github.com/agent-infra/sandbox/blob/7f1afaf8d82bd30531a19caeb1a24dfebbc97d8c/examples/langgraph-deepagents-integration/sandbox_backend.py),
commit `7f1afaf8d82bd30531a19caeb1a24dfebbc97d8c`, licensed under Apache License 2.0.
The license is included in `LICENSES/agent-infra-sandbox-Apache-2.0.txt`.
The repository root has no NOTICE file at this fixed revision.

Modifications: structured DeepAgents 0.7.32 file results, POSIX paths,
CubeProxy configuration, response validation, explicit timeouts, no automatic
retries, serialized shell lifecycle, credential redaction, and
unknown-execution-state errors. File operations use the agent-sandbox SDK;
glob reuses BaseSandbox.glob to match the tool schema's pattern semantics.
Async forwarding is inherited from DeepAgents SandboxBackendProtocol.
Gate action serialization and graph cancellation handling are shared with
the local backend. This is a maintained adaptation, not an unmodified example.
