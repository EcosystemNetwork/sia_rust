# Superradiant backend image (Railway).
# Builds the `sia` binary with the credential store + house competitors, then
# ships it with python3 (benchmark scoring shells out to each task's evaluate.py)
# and the bundled task tree.

FROM rust:1-bookworm AS build
WORKDIR /app
COPY . .
RUN cargo build --release --features superradiant-db

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app/target/release/sia /usr/local/bin/sia
# Benchmark task dirs (task.md + evaluate.py) are read at runtime for scoring.
COPY --from=build /app/sia ./sia
ENV SUPERRADIANT_PYTHON=python3
# Railway injects $PORT; the server binds 0.0.0.0:$PORT.
EXPOSE 8000
CMD ["sia", "superradiant", "--runs-dir", "/app/runs"]
