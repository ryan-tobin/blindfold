# Phala's dstack simulator: the same API as a real enclave's dstack.sock, with
# fixed sample keys and a sample quote. Built from source at a pinned tag.
# Adapted from dstack's dstack/gateway/test-run/attestation/Dockerfile.simulator (Apache 2.0).
FROM rust:1.92-bookworm AS builder
RUN git clone --depth 1 --branch v0.6.1 https://github.com/Dstack-TEE/dstack /src \
    && test "$(git -C /src rev-parse HEAD)" = 7f143148cc49f4a09799958835d310e050137cb3
WORKDIR /src
RUN cargo build --manifest-path dstack/Cargo.toml --locked --release -p dstack-guest-agent-simulator \
    && cp dstack/target/release/dstack-simulator /usr/local/bin/

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/dstack-simulator
WORKDIR /opt/dstack-simulator
COPY --from=builder /usr/local/bin/dstack-simulator /usr/local/bin/dstack-simulator
COPY --from=builder /src/sdk/simulator/app-compose.json /src/sdk/simulator/appkeys.json \
    /src/sdk/simulator/sys-config.json /src/sdk/simulator/attestation.bin ./
COPY simulator.toml ./
CMD ["dstack-simulator", "--config", "/opt/dstack-simulator/simulator.toml"]