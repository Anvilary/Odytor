#!/bin/bash
# Lifecycle of the dedicated Git Plus runner VM on hosting-bay. No root needed: QEMU runs in an unprivileged
# container that holds /dev/kvm and one private bridge network; the guest has its own kernel.
#
#   runner-vm.sh init        keys, base image, qemu image, network, disk, cloud-init seed (idempotent)
#   runner-vm.sh start       start the VM container (restarts with Docker)
#   runner-vm.sh stop        ask the guest to power off, then stop the container
#   runner-vm.sh status      container state and the guest's SSH host key fingerprint
#   runner-vm.sh ssh [args]  administrative SSH into the guest (vmadmin)
#   runner-vm.sh provision   copy deploy/runner + wheel and run provision.sh in the guest. SSH is allowed only from
#                            the VM's private Docker network (its gateway, i.e. the host, and attached containers).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="${GP_VM_BASE:-$HOME/services/git-plus-runner-vm}"
NET="${GP_VM_NET:-gp-runner-net}"; SUBNET="${GP_VM_SUBNET:-172.30.77.0/28}"; GUEST_IP="${GP_VM_IP:-172.30.77.2}"
CT=git-plus-runner-vm; IMG=git-plus-runner-qemu:1
VM_MEM="${GP_VM_MEM_MIB:-3072}"; VM_CPUS="${GP_VM_CPUS:-2}"; DISK="${GP_VM_DISK:-40G}"
BASE_URL=https://cloud.debian.org/images/cloud/trixie/latest

ssh_admin() { ssh -i "$BASE/keys/admin" -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile="$BASE/keys/known_hosts" \
  -o StrictHostKeyChecking=yes -o ConnectTimeout=10 -p 2222 "vmadmin@$GUEST_IP" "$@"; }
qemu_run() { docker run --rm --user "$(id -u):$(id -g)" -v "$BASE:/vm" -w /vm "$IMG" "$@"; }

case "${1:-}" in
init)
  mkdir -p "$BASE/keys" "$BASE/disk"; chmod 700 "$BASE" "$BASE/keys"
  for k in admin runner host; do [ -f "$BASE/keys/$k" ] || ssh-keygen -q -t ed25519 -N '' -C "git-plus-runner-$k" -f "$BASE/keys/$k"; done
  chmod 600 "$BASE/keys/runner" "$BASE/keys/admin" "$BASE/keys/host"; chmod 644 "$BASE/keys/runner.pub"
  if [ ! -f "$BASE/disk/base.qcow2" ]; then
    (cd "$BASE/disk" && curl -fsSLO "$BASE_URL/SHA512SUMS" && curl -fsSL -o base.qcow2 "$BASE_URL/debian-13-genericcloud-amd64.qcow2" \
      && grep ' debian-13-genericcloud-amd64.qcow2$' SHA512SUMS | sed 's/debian-13-genericcloud-amd64.qcow2/base.qcow2/' | sha512sum -c -)
  fi
  docker image inspect "$IMG" >/dev/null 2>&1 || docker build -q -t "$IMG" "$HERE/qemu"
  docker network inspect "$NET" >/dev/null 2>&1 || docker network create --driver bridge --subnet "$SUBNET" --gateway "${SUBNET%.*}.1" \
    --opt com.docker.network.bridge.name=br-gp-runner "$NET" >/dev/null
  [ -f "$BASE/disk/vm.qcow2" ] || qemu_run qemu-img create -q -f qcow2 -F qcow2 -b base.qcow2 disk/vm.qcow2 "$DISK"
  if [ ! -f "$BASE/disk/seed.iso" ]; then
    {
      echo '#cloud-config'; echo 'users:'; echo '  - name: vmadmin'; echo '    sudo: ALL=(ALL) NOPASSWD:ALL'; echo '    shell: /bin/bash'
      echo '    lock_passwd: true'; echo '    ssh_authorized_keys:'; echo "      - $(cat "$BASE/keys/admin.pub")"
      echo 'ssh_deletekeys: true'; echo 'ssh_pwauth: false'; echo 'ssh_keys:'; echo '  ed25519_private: |'; sed 's/^/    /' "$BASE/keys/host"
      echo "  ed25519_public: $(cat "$BASE/keys/host.pub")"
    } > "$BASE/disk/user-data"
    printf 'instance-id: gp-runner-1\nlocal-hostname: gp-runner\n' > "$BASE/disk/meta-data"
    qemu_run xorriso -as mkisofs -quiet -o disk/seed.iso -V cidata -J -r disk/user-data disk/meta-data
    rm -f "$BASE/disk/user-data"
  fi
  # the guest's host key is pinned by the client, by name and by address
  printf '[%s]:2222 %s\n[%s]:2222 %s\n' "$GUEST_IP" "$(cut -d' ' -f1,2 "$BASE/keys/host.pub")" "$CT" "$(cut -d' ' -f1,2 "$BASE/keys/host.pub")" > "$BASE/keys/known_hosts"
  chmod 644 "$BASE/keys/known_hosts"; echo "init ok: $BASE" ;;
start)
  docker rm -f "$CT" >/dev/null 2>&1 || true
  docker run -d --name "$CT" --restart unless-stopped --network "$NET" --ip "$GUEST_IP" --network-alias "$CT" \
    --user "$(id -u):$(id -g)" --group-add "$(stat -c %g /dev/kvm)" --device /dev/kvm \
    --cap-drop ALL --security-opt no-new-privileges:true --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m \
    --memory "$((VM_MEM + 512))m" --memory-swap "$((VM_MEM + 512))m" --cpus "$VM_CPUS" --pids-limit 256 \
    -v "$BASE/disk:/vm:rw" "$IMG" qemu-system-x86_64 -enable-kvm -cpu host -smp "$VM_CPUS" -m "$VM_MEM" -name gp-runner \
    -display none -serial file:/tmp/console.log -monitor none \
    -drive file=/vm/vm.qcow2,if=virtio,cache=none,aio=native,discard=unmap \
    -drive file=/vm/seed.iso,if=virtio,media=cdrom,readonly=on,format=raw \
    -netdev user,id=n0,hostfwd=tcp::2222-:22 -device virtio-net-pci,netdev=n0 -device virtio-rng-pci >/dev/null
  echo "started $CT at $GUEST_IP:2222" ;;
stop)
  ssh_admin 'sudo systemctl poweroff' >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do [ "$(docker inspect -f '{{.State.Running}}' "$CT" 2>/dev/null)" = true ] || break; sleep 2; done
  docker update --restart=no "$CT" >/dev/null 2>&1 || true; docker stop -t 20 "$CT" >/dev/null 2>&1 || true
  docker update --restart=unless-stopped "$CT" >/dev/null 2>&1 || true; echo stopped ;;
status)
  docker ps -a --filter "name=^$CT$" --format '{{.Names}} {{.Status}}'
  docker stats --no-stream --format '{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}}' "$CT" 2>/dev/null || true ;;
ssh) shift; ssh_admin "$@" ;;
provision)
  : "${GP_RUNNER_IMAGE:?pin the runner image by digest}" "${GP_WHEEL:?path to the git_plus wheel}"
  TMP="$(mktemp -d)"; cp -r "$HERE/../runner" "$TMP/runner"; cp "$GP_WHEEL" "$TMP/"; cp "$BASE/keys/runner.pub" "$TMP/"
  ssh_admin 'rm -rf /tmp/gp-prov && mkdir -p /tmp/gp-prov'
  scp -q -r -i "$BASE/keys/admin" -o IdentitiesOnly=yes -o UserKnownHostsFile="$BASE/keys/known_hosts" -o StrictHostKeyChecking=yes \
    -P 2222 "$TMP"/. "vmadmin@$GUEST_IP:/tmp/gp-prov/"
  ssh_admin "cd /tmp/gp-prov && sudo env GP_RUNNER_IMAGE='$GP_RUNNER_IMAGE' GP_AUTHORIZED_KEY_FILE=/tmp/gp-prov/runner.pub GP_WHEEL=/tmp/gp-prov/$(basename "$GP_WHEEL") GP_ALLOW_FROM=$SUBNET ${GP_PIP_INDEX_URL:+GP_PIP_INDEX_URL=$GP_PIP_INDEX_URL} bash runner/provision.sh"
  rm -rf "$TMP" ;;
*) sed -n '2,10p' "$0"; exit 2 ;;
esac
