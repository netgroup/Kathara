import io
import tarfile

# Scripts that a device with its own UML kernel (lab.conf: dev[kernel]="/path/to/linux") runs. They are copied into
# the container at creation, in /kathara-uml: start is the entrypoint of the container, init the PID 1 of the UML,
# ushell the shell of the device (startup, exec and connect go into the UML through it).

START = r"""#!/bin/bash
# Kathara UML device, entrypoint of the container. Waits until Kathara has
# attached every interface (it touches /run/kathara-uml.ready), adds a
# management veth (um0 here, um1 for the UML), then boots the UML kernel
# on the container's own rootfs (hostfs), one vector raw interface per
# ethN plus one on um1. The console goes to a file and stdin stays open:
# with a pipe on stdout or stdin at EOF the UML boot stops.

KEY=/var/lib/kathara-uml/id
UML_TMP=/tmp/uml
CONSOLE_IN=$UML_TMP/stdin

# Kathara attaches the interfaces after the first one once the container
# runs, then touches the ready file. Waits for it up to 60 s.
wait_for_interfaces()
{
	local tries=600

	while [ ! -e /run/kathara-uml.ready ] && [ "$tries" -gt 0 ]; do
		sleep 0.1
		tries=$((tries - 1))
	done
}

# Hands an interface of the container over to the UML: up, without IPv6,
# and a vector raw interface vecN on it in the boot options.
add_vector()
{
	local n=$1 ifc=$2

	sysctl -wq "net.ipv6.conf.$ifc.disable_ipv6=1" 2>/dev/null
	ip link set "$ifc" up
	vectors+=("vec$n:transport=raw,ifname=$ifc,depth=128,gro=1")
}

# The key of ushell, made first: Kathara may run the startup through
# ushell as soon as the interfaces are attached. The UML reads
# authorized_keys through hostfs.
make_key()
{
	mkdir -p "$(dirname "$KEY")" /root/.ssh
	if [ ! -f "$KEY" ]; then
		ssh-keygen -q -t ed25519 -N '' -f "$KEY"
	fi
	cp "$KEY.pub" /root/.ssh/authorized_keys
}

make_key
wait_for_interfaces

ip link add um0 type veth peer name um1
ip addr add 169.254.77.1/30 dev um0

vectors=()
n=${KATHARA_UML_IFACES:-0}
for ((k = 0; k < n; k++)); do
	add_vector "$k" "eth$k"
done
add_vector "$n" um1
ip link set um0 up

ifaces=(/sys/class/net/*)
echo "start: ${ifaces[*]##*/} -> ${vectors[*]}" > /var/log/uml-start.log

# The stdin of the console: a FIFO that sleep keeps open.
mkdir -p "$UML_TMP"
rm -f "$CONSOLE_IN"
mkfifo "$CONSOLE_IN"
sleep infinity > "$CONSOLE_IN" &

exec env TMPDIR="$UML_TMP" /kathara-uml-kernel mem="${KATHARA_UML_MEM:-256M}" \
	root=/dev/root rootfstype=hostfs rootflags=/ rw init=/kathara-uml/init \
	con0=fd:0,fd:1 con=null "${vectors[@]}" \
	< "$CONSOLE_IN" > /var/log/uml-console.log 2>&1
"""

INIT = r"""#!/bin/bash
# Kathara UML device, PID 1 inside the UML: mounts, the interfaces named
# as Kathara names them (vecK -> ethK, the last one -> kmgmt), sshd for
# /kathara-uml/ushell, then a status file the container can read.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

STATUS=/var/tmp/uml-status

mount_filesystems()
{
	mount -t proc proc /proc
	mount -t sysfs sysfs /sys
	mount -t devtmpfs devtmpfs /dev 2>/dev/null
	mkdir -p /dev/pts
	mount -t devpts devpts /dev/pts
	mount -t tmpfs tmpfs /run
	mount -t bpf bpf /sys/fs/bpf
}

# The vector interfaces are vec0 ... vecN: one per interface of the
# container, in order, and the last one on the management veth.
name_interfaces()
{
	local vectors=(/sys/class/net/vec*) n k

	n=${#vectors[@]}
	for ((k = 0; k < n - 1; k++)); do
		ip link set "vec$k" name "eth$k"
		ip link set "eth$k" up
	done
	ip link set "vec$((n - 1))" name kmgmt
	ip addr add 169.254.77.2/30 dev kmgmt
	ip link set kmgmt up
}

write_status()
{
	{
		echo "UML $(uname -r) up $(cut -d' ' -f1 /proc/uptime)"
		ip -br addr
		cat /proc/net/dev
	} > "$STATUS.new"
	mv "$STATUS.new" "$STATUS"
}

mount_filesystems
hostname "$(cat /etc/hostname 2>/dev/null || echo uml)"
ip link set lo up
name_interfaces

mkdir -p /run/sshd
/usr/sbin/sshd -o ListenAddress=169.254.77.2 -o UseDNS=no -o PermitRootLogin=prohibit-password

while true; do
	write_status
	sleep 2
done
"""

USHELL = r"""#!/bin/bash
# Shell of a Kathara UML device. Kathara runs the startup as `shell -c
# CMDS`, exec as `shell -c CMD` and connect as `shell`: all go into the
# UML through one ssh on the management veth.
# While the UML boots, ssh retries only the TCP connect, once a second
# (ConnectionAttempts), so a command never runs twice. ConnectTimeout
# bounds each connect and the ssh banner exchange.

KEY=/var/lib/kathara-uml/id
SSH=(ssh -q -i "$KEY"
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR
	-o ConnectionAttempts=120 -o ConnectTimeout=10
	root@169.254.77.2)

# start, the entrypoint of the container, makes the key when it begins,
# and a startup that Kathara runs right away may get here first. Waits
# for the public key up to 60 s: ssh-keygen writes it after the private
# one.
wait_for_key()
{
	local tries=600

	while [ ! -e "$KEY.pub" ] && [ "$tries" -gt 0 ]; do
		sleep 0.1
		tries=$((tries - 1))
	done
}

wait_for_key
if [ "$1" = -c ]; then
	shift
	exec "${SSH[@]}" -T -- "$*"
fi
exec "${SSH[@]}" -t
"""


def uml_helpers_tar() -> bytes:
    """Return a tar archive with the UML helpers in kathara-uml/, executable, to be extracted in `/`.

    Returns:
        bytes: The tar archive.
    """
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tar:
        directory = tarfile.TarInfo("kathara-uml")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        tar.addfile(directory)
        for name, content in (("start", START), ("init", INIT), ("ushell", USHELL)):
            encoded = content.encode("utf-8")
            info = tarfile.TarInfo("kathara-uml/" + name)
            info.size = len(encoded)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(encoded))
    return data.getvalue()
