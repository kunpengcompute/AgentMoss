FROM cube-sandbox-image.tencentcloudcr.com/demo/aio-sandbox-envd-openclaw@sha256:67bb4b374ecbc7f4855dfa60437259f89364c3863304b8cac8770421ad9561bf

USER root
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG AIO_PYTHON=/opt/python3.12/bin/python3
ENV AGENT_MOSS_DISABLE_LLM=1 AGENT_MOSS_RULES_MODE=builtin DISABLE_OPENCLAW=true

# Build context is the repository root; Dockerfile.dockerignore selects source files.
COPY AgentMoss/ /opt/agentmoss-source/AgentMoss/
RUN PIP_INDEX_URL="${PIP_INDEX_URL}" "${AIO_PYTHON}" \
    /opt/agentmoss-source/AgentMoss/integrations/cubesandbox/image/install_in_aio.py \
    --python "${AIO_PYTHON}" \
    && rm -rf /opt/agentmoss-source

LABEL org.agentmoss.integration="aio-shell-inprocess" org.agentmoss.aio="1.11.0-arm64"
