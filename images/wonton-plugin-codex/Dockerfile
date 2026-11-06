# Agent 层：固定版本的 codex-acp（Zed 的 ACP 适配器）和 Codex CLI（官方 npm 包，登录用设备码方式），
# 加一个 stdio↔unix socket 转发器。codex-acp 只从 stdin/stdout 说 ACP，转发器让桥能跨容器连进来。
FROM public.ecr.aws/docker/library/debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

ARG TARGETARCH
# 两个包都取官方 npm registry，按版本和 SHA-512（npm dist.integrity 的十六进制）固定。
ARG CODEX_ACP_VERSION=0.16.0
ARG CODEX_ACP_X64_SHA512=c6ce736412e926e72211b64dc7a65234bd2a09af61de2ff35a98f8d2ca7a42d17e2f83b0ffbab31dd0736e81a11dd733d63acb79d7d97911430361258fc4cb30
ARG CODEX_ACP_ARM64_SHA512=f0769919654f56cd4deb2a8898b08a96795c61361cf41302121695264d0e37d972a1f84af663810401d09dd282e1272aab924b5074083b26fcfa3c0751ede149
ARG CODEX_CLI_VERSION=0.137.0
ARG CODEX_CLI_X64_SHA512=9fedba3148fcade91b10351e6131a80fa1d7b864b43262c79f62ce9f48b9a93358209bd757cd81edc08b493cd5285fd127144c465db6b1ef50d19d37e492159e
ARG CODEX_CLI_ARM64_SHA512=477659ca6410035aa9a7a3a9a30378f57278b1c1f049c92aec28d5be098bbf76c8b375fe1745d72b7c4f1640bdbecda65f7c0f7a43e2dce369c71fb23c0b09e9

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl python3 \
    && rm -rf /var/lib/apt/lists/*
RUN case "$TARGETARCH" in \
      amd64) acp_pkg=codex-acp-linux-x64; acp_sha="$CODEX_ACP_X64_SHA512"; triple=x86_64-unknown-linux-musl; cli_pkg=${CODEX_CLI_VERSION}-linux-x64; cli_sha="$CODEX_CLI_X64_SHA512" ;; \
      arm64) acp_pkg=codex-acp-linux-arm64; acp_sha="$CODEX_ACP_ARM64_SHA512"; triple=aarch64-unknown-linux-musl; cli_pkg=${CODEX_CLI_VERSION}-linux-arm64; cli_sha="$CODEX_CLI_ARM64_SHA512" ;; \
      *) echo "不支持的架构 $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && mkdir -p /tmp/acp /tmp/cli \
    && curl --fail --silent --show-error --location \
         "https://registry.npmjs.org/@zed-industries/${acp_pkg}/-/${acp_pkg}-${CODEX_ACP_VERSION}.tgz" \
         --output /tmp/acp.tgz \
    && echo "$acp_sha  /tmp/acp.tgz" | sha512sum --check --strict \
    && curl --fail --silent --show-error --location \
         "https://registry.npmjs.org/@openai/codex/-/codex-${cli_pkg}.tgz" \
         --output /tmp/cli.tgz \
    && echo "$cli_sha  /tmp/cli.tgz" | sha512sum --check --strict \
    && tar -xzf /tmp/acp.tgz -C /tmp/acp \
    && tar -xzf /tmp/cli.tgz -C /tmp/cli \
    && install -m 0755 "/tmp/acp/package/bin/codex-acp" /usr/local/bin/codex-acp \
    && install -m 0755 "/tmp/cli/package/vendor/${triple}/bin/codex" /usr/local/bin/codex \
    && install -m 0755 "/tmp/cli/package/vendor/${triple}/codex-path/rg" /usr/local/bin/rg \
    && rm -rf /tmp/acp* /tmp/cli* \
    && mkdir -p /data/agent/home /workspace /data/run /usr/local/share/wonton-codex/skills /usr/share/doc/wonton-codex \
    && groupadd --system --gid 65532 wonton \
    && useradd --system --uid 65532 --gid 65532 --no-create-home wonton \
    && chown -R 65532:65532 /data/agent /workspace /data/run

COPY agent-entrypoint.py /usr/local/bin/wonton-codex-agent
COPY LICENSE.codex-acp LICENSE.codex /usr/share/doc/wonton-codex/
COPY prompt.md /usr/local/share/wonton-codex/prompt.md
COPY wonton-help /usr/local/share/wonton-codex/skills/wonton-help
RUN chmod 0755 /usr/local/bin/wonton-codex-agent \
    && chmod 0644 /usr/share/doc/wonton-codex/LICENSE.codex-acp /usr/share/doc/wonton-codex/LICENSE.codex \
       /usr/local/share/wonton-codex/prompt.md /usr/local/share/wonton-codex/skills/wonton-help/SKILL.md

ENV HOME=/data/agent/home \
    WONTON_ACP_SOCKET=/data/run/acp.sock \
    WONTON_AGENT_WORKSPACE=/workspace

USER 65532:65532
ENTRYPOINT ["/usr/local/bin/wonton-codex-agent"]
