#!/bin/bash
# Provision a dedicated Git Plus test-runner host (Debian 13). Run as root. Idempotent.
#
#   GP_RUNNER_IMAGE=docker.io/library/python@sha256:<digest> \
#   GP_AUTHORIZED_KEY_FILE=/root/git-plus-runner.pub \
#   GP_WHEEL=/root/git_plus-0.1.0-py3-none-any.whl \
#   GP_ALLOW_FROM=203.0.113.10 \
#   ./provision.sh
#
# GP_ALLOW_FROM  address of the Git Plus host; only it may reach sshd (strongly recommended)
# GP_DNS         space-separated resolvers the account may use (default: from /etc/resolv.conf)
# GP_PIP_INDEX_URL  optional package index/mirror for dependency preparation
# GP_JOB_MEMORY_MIB per-job memory limit (default: 60% of this host's RAM, so a job can never exhaust the guest)
set -euo pipefail

: "${GP_RUNNER_IMAGE:?pin the image by digest, e.g. docker.io/library/python@sha256:...}"
: "${GP_AUTHORIZED_KEY_FILE:?public key of the dedicated Git Plus runner identity}"
: "${GP_WHEEL:?path to the git_plus wheel}"
GP_ALLOW_FROM="${GP_ALLOW_FROM:-}"
GP_PIP_INDEX_URL="${GP_PIP_INDEX_URL:-}"
GP_USER=gp-runner
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd /   # the runner account must not inherit an unreadable working directory

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
[[ "$GP_RUNNER_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]] || { echo "GP_RUNNER_IMAGE must be pinned by digest" >&2; exit 1; }
[ "$(wc -l < "$GP_AUTHORIZED_KEY_FILE")" -le 1 ] && grep -qE '^ssh-ed25519 [A-Za-z0-9+/=]+( .*)?$' "$GP_AUTHORIZED_KEY_FILE" \
  || { echo "GP_AUTHORIZED_KEY_FILE must hold exactly one ssh-ed25519 public key" >&2; exit 1; }
[[ -z "$GP_ALLOW_FROM" || "$GP_ALLOW_FROM" =~ ^[0-9a-fA-F:.]+(/[0-9]+)?$ ]] || { echo "invalid GP_ALLOW_FROM" >&2; exit 1; }

echo "== packages"
export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8
apt-get update -qq
apt-get install -y -qq --no-install-recommends podman uidmap passt crun netavark aardvark-dns dbus-user-session \
  openssh-server nftables python3 python3-venv ca-certificates >/dev/null

echo "== unprivileged account"
if ! id "$GP_USER" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash --user-group "$GP_USER"
fi
passwd -l "$GP_USER" >/dev/null
gpasswd -d "$GP_USER" sudo >/dev/null 2>&1 || true
if ! grep -q "^$GP_USER:" /etc/subuid; then usermod --add-subuids 200000-265535 "$GP_USER"; fi
if ! grep -q "^$GP_USER:" /etc/subgid; then usermod --add-subgids 200000-265535 "$GP_USER"; fi
GP_UID=$(id -u "$GP_USER")
loginctl enable-linger "$GP_USER"

echo "== cgroup delegation (cpu/memory/pids limits for rootless containers)"
install -d /etc/systemd/system/user@.service.d
printf '[Service]\nDelegate=cpu cpuset io memory pids\n' > /tmp/gp-delegate.conf
if ! cmp -s /tmp/gp-delegate.conf /etc/systemd/system/user@.service.d/git-plus-runner.conf; then
  install -m 0644 /tmp/gp-delegate.conf /etc/systemd/system/user@.service.d/git-plus-runner.conf
  systemctl daemon-reload
  systemctl restart "user@${GP_UID}.service"   # only when the delegation changed: this stops Podman and any running job
fi
rm -f /tmp/gp-delegate.conf

echo "== runner software (root-owned, not writable by the runner account)"
install -d -m 0755 /opt/git-plus-runner
[ -x /opt/git-plus-runner/venv/bin/python ] || python3 -m venv /opt/git-plus-runner/venv
/opt/git-plus-runner/venv/bin/pip install --quiet --no-deps --force-reinstall "$GP_WHEEL"
install -m 0755 -o root -g root "$HERE/git-plus-runner-entry" /usr/local/libexec/git-plus-runner-entry

echo "== configuration"
install -d -m 0755 -o root -g root /etc/git-plus-runner
{
  echo "# managed by provision.sh"
  echo "GIT_PLUS_RUNNER_IMAGE=$GP_RUNNER_IMAGE"
  [ -z "$GP_PIP_INDEX_URL" ] || echo "GIT_PLUS_RUNNER_PIP_INDEX_URL=$GP_PIP_INDEX_URL"
  if [ -n "${GP_JOB_MEMORY_MIB:-}" ]; then echo "GIT_PLUS_RUNNER_MEMORY_MIB=$GP_JOB_MEMORY_MIB"
  elif ! grep -q '^GIT_PLUS_RUNNER_MEMORY_MIB=' /etc/git-plus-runner/runner.env 2>/dev/null; then
    awk '/^MemTotal:/ {printf "GIT_PLUS_RUNNER_MEMORY_MIB=%d\n", $2/1024*0.6}' /proc/meminfo
  fi
  # keep administrator tuning that is not managed here (the job memory limit only when it was not set explicitly above)
  keep='CPUS|PIDS|WORK_MIB|TMP_MIB|MAX_TIMEOUT|PREP_TIMEOUT|FRAME_TIMEOUT|MAX_SOURCE_MIB|ENV_TTL_DAYS|ENV_BUDGET_MIB|MIN_FREE_MIB|ROOT|MAX_QUEUED|QUEUE_WAIT'
  [ -n "${GP_JOB_MEMORY_MIB:-}" ] || keep="$keep|MEMORY_MIB"
  [ ! -f /etc/git-plus-runner/runner.env ] || grep -E "^GIT_PLUS_RUNNER_($keep)=" /etc/git-plus-runner/runner.env || true
} > /etc/git-plus-runner/runner.env.new
install -m 0644 -o root -g root /etc/git-plus-runner/runner.env.new /etc/git-plus-runner/runner.env
rm -f /etc/git-plus-runner/runner.env.new

echo "== restricted SSH"
key="$(head -n1 "$GP_AUTHORIZED_KEY_FILE")"
opts='restrict,command="/usr/local/libexec/git-plus-runner-entry"'
[ -z "$GP_ALLOW_FROM" ] || opts="from=\"$GP_ALLOW_FROM\",$opts"
printf '%s %s\n' "$opts" "$key" > /etc/ssh/git-plus-runner.authorized_keys
chmod 0644 /etc/ssh/git-plus-runner.authorized_keys
install -m 0644 "$HERE/sshd-git-plus-runner.conf" /etc/ssh/sshd_config.d/60-git-plus-runner.conf
grep -qE '^\s*Include\s+/etc/ssh/sshd_config.d/' /etc/ssh/sshd_config \
  || sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' /etc/ssh/sshd_config
sshd -t
systemctl enable --now ssh >/dev/null 2>&1 || true
systemctl reload ssh

echo "== network policy"
GP_DNS="${GP_DNS:-$(awk '/^nameserver/ {print $2}' /etc/resolv.conf | grep -E '^[0-9.]+$' | tr '\n' ' ')}"
dns_set="127.0.0.0/8"
for ns in $GP_DNS; do dns_set="$dns_set, $ns"; done
ssh_rule='tcp dport 22 accept'
[ -z "$GP_ALLOW_FROM" ] || ssh_rule="ip saddr $GP_ALLOW_FROM tcp dport 22 accept"
cat > /etc/git-plus-runner/nftables.conf <<NFT
# Ingress: SSH only from the Git Plus host. Egress of the runner account (dependency preparation only):
# the package index and DNS, never private, link-local or carrier-grade NAT networks.
table inet git_plus_runner
delete table inet git_plus_runner
table inet git_plus_runner {
  chain input {
    type filter hook input priority 0; policy drop;
    ct state established,related accept
    iif lo accept
    meta l4proto { icmp, ipv6-icmp } accept
    udp sport 67 udp dport 68 accept
    $ssh_rule
  }
  chain output {
    type filter hook output priority 0; policy accept;
    meta skuid $GP_UID ip daddr { $dns_set } accept
    meta skuid $GP_UID ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, 100.64.0.0/10, 127.0.0.0/8 } drop
    meta skuid $GP_UID ip6 daddr { fc00::/7, fe80::/10, ::1 } drop
  }
}
NFT
nft -c -f /etc/git-plus-runner/nftables.conf
grep -q 'git-plus-runner/nftables.conf' /etc/nftables.conf || echo 'include "/etc/git-plus-runner/nftables.conf"' >> /etc/nftables.conf
nft -f /etc/git-plus-runner/nftables.conf
systemctl enable nftables >/dev/null 2>&1

echo "== pinned image"
runas() { runuser -u "$GP_USER" -- env XDG_RUNTIME_DIR="/run/user/$GP_UID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$GP_UID/bus" HOME="/home/$GP_USER" "$@"; }
runas podman system migrate >/dev/null 2>&1 || true   # clears a stale pause process left by a user-manager restart
runas podman image exists "$GP_RUNNER_IMAGE" || runas podman pull --quiet "$GP_RUNNER_IMAGE" >/dev/null

echo "== housekeeping timer"
install -m 0644 "$HERE/git-plus-runner-gc.service" "$HERE/git-plus-runner-gc.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now git-plus-runner-gc.timer >/dev/null 2>&1

echo "== validation"
runuser -u "$GP_USER" -- /usr/local/libexec/git-plus-runner-entry check
echo "provisioned"
