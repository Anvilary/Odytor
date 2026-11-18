# QEMU/KVM for the Git Plus runner VM. Runs unprivileged: no root, no capabilities, only /dev/kvm.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends qemu-system-x86 qemu-utils xorriso ca-certificates \
    && rm -rf /var/lib/apt/lists/*
