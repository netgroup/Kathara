import io
import tarfile
from typing import Dict, Union

# Scripts that a device with its own UML kernel (lab.conf: dev[kernel]="/path/to/linux") runs. They are copied into
# the container at creation, in /kathara-uml: start is the entrypoint of the container, init the PID 1 of the UML,
# ushell the shell of the device (startup, exec and connect go into the UML through it). With them go the settings
# of the device (UmlSettings): sysctl.conf, the sysctls that Kathara gives the container, which init applies to the
# UML kernel, environment, the variables of the device, which ushell exports in the UML, shell, the shell of the
# device, in which ushell runs the commands and which connect opens, and ulimit.conf, the ulimits of the device,
# which init sets before it starts sshd.

START = r"""#!/bin/bash
# Kathara UML device, entrypoint of the container. Waits until Kathara has
# attached every interface (it touches /run/kathara-uml.ready), adds a
# management veth (um0 here, um1 for the UML), then boots the UML kernel
# on the container's own rootfs (hostfs), one vector raw interface per
# ethN plus one on um1. The console goes to a file and stdin stays open:
# with a pipe on stdout or stdin at EOF the UML boot stops.
# KATHARA_UML_SECCOMP (dev[env]) is the seccomp= option of UML: auto (the
# default here) uses the seccomp mode where it works and falls back to
# ptrace (the kernel default, off); the console log says which one ran.
# Each UML interface takes the MAC address that Kathara gave the interface
# of the container. The bridged interface of the device, if any
# (KATHARA_UML_BRIDGED_IFACE), goes to the UML too, with the address and
# the default route that Docker gave it. The memory of the UML is
# KATHARA_UML_MEM, else three quarters of the memory limit of the
# container, else 256M.

KEY=/var/lib/kathara-uml/id
BRIDGED_CONF=/var/lib/kathara-uml/bridged.conf
# The management link: link-local, in a range a lab does not use, and
# reserved in a UML device, whose stack holds it next to the interfaces of
# the lab. The same in init and ushell.
MGMT_CONTAINER=169.254.77.1/30
UML_TMP=/tmp/uml
CONSOLE_IN=$UML_TMP/stdin

# Docker mounts /etc/hosts and /etc/resolv.conf in a container, and the
# startup of Kathara unmounts them first, so that it finds the files of
# the image. That startup runs inside the UML, where they are not mounts:
# unmount them here, in the container, before the UML boots.
unmount_docker_files()
{
	umount /etc/resolv.conf 2>/dev/null
	umount /etc/hosts 2>/dev/null
}

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

# A random locally administered unicast MAC address.
random_mac()
{
	printf '02:%02x:%02x:%02x:%02x:%02x\n' $((RANDOM % 256)) $((RANDOM % 256)) \
		$((RANDOM % 256)) $((RANDOM % 256)) $((RANDOM % 256))
}

# Prints the MAC address of an interface of the container, the one Kathara
# gave it, for the UML, and gives the interface a random one: the stack of
# the container then takes the frames for the UML as not its own.
take_mac()
{
	local ifc=$1

	cat "/sys/class/net/$ifc/address"
	ip link set "$ifc" down
	ip link set "$ifc" address "$(random_mac)"
}

# Hands an interface of the container over to the UML: up, without IPv6,
# and a vector raw interface vecN on it in the boot options, with the MAC
# address mac if given.
add_vector()
{
	local n=$1 ifc=$2 mac=$3 option

	option="vec$n:transport=raw,ifname=$ifc,depth=128,gro=1"
	if [ -n "$mac" ]; then
		option+=",mac=$mac"
	fi
	sysctl -wq "net.ipv6.conf.$ifc.disable_ipv6=1" 2>/dev/null
	ip link set "$ifc" up
	vectors+=("$option")
}

# Moves the IPv4 address and the default route that Docker gave the bridged
# interface of the container to BRIDGED_CONF, which init reads: only the
# UML must answer on that address.
take_bridged_config()
{
	local ifc=$1 address gateway

	address=$(ip -o -4 addr show dev "$ifc" | awk '{ print $4 }')
	gateway=$(ip -4 route show default dev "$ifc" | awk '{ print $3 }')
	printf 'ifname=%s\naddress=%s\ngateway=%s\n' "$ifc" "$address" "$gateway" > "$BRIDGED_CONF"
	ip -4 route flush dev "$ifc"
	ip -4 addr flush dev "$ifc"
}

# The memory of the UML: KATHARA_UML_MEM, else three quarters of the memory
# limit of the container (cgroup v2), the rest left to UML itself, else
# 256M.
uml_memory()
{
	local limit

	if [ -n "$KATHARA_UML_MEM" ]; then
		echo "$KATHARA_UML_MEM"
		return
	fi
	limit=$(cat /sys/fs/cgroup/memory.max 2>/dev/null)
	if [[ $limit =~ ^[0-9]+$ ]]; then
		echo "$((limit * 3 / 4 / 1048576))M"
	else
		echo 256M
	fi
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
unmount_docker_files
wait_for_interfaces

ip link add um0 type veth peer name um1
ip addr add "$MGMT_CONTAINER" dev um0

vectors=()
n=${KATHARA_UML_IFACES:-0}
for ((k = 0; k < n; k++)); do
	add_vector "$k" "eth$k" "$(take_mac "eth$k")"
done
rm -f "$BRIDGED_CONF"
if [ -n "$KATHARA_UML_BRIDGED_IFACE" ]; then
	bridged=eth$KATHARA_UML_BRIDGED_IFACE
	take_bridged_config "$bridged"
	add_vector "${#vectors[@]}" "$bridged" "$(take_mac "$bridged")"
fi
add_vector "${#vectors[@]}" um1
ip link set um0 up

seccomp=${KATHARA_UML_SECCOMP:-auto}
mem=$(uml_memory)
ifaces=(/sys/class/net/*)
echo "start: ${ifaces[*]##*/} -> ${vectors[*]} seccomp=$seccomp mem=$mem" > /var/log/uml-start.log

# The stdin of the console: a FIFO that sleep keeps open.
mkdir -p "$UML_TMP"
rm -f "$CONSOLE_IN"
mkfifo "$CONSOLE_IN"
sleep infinity > "$CONSOLE_IN" &

exec env TMPDIR="$UML_TMP" /kathara-uml-kernel mem="$mem" \
	root=/dev/root rootfstype=hostfs rootflags=/ rw init=/kathara-uml/init \
	con0=fd:0,fd:1 con=null seccomp="$seccomp" "${vectors[@]}" \
	< "$CONSOLE_IN" > /var/log/uml-console.log 2>&1
"""

INIT = r"""#!/bin/bash
# Kathara UML device, PID 1 inside the UML: mounts, the interfaces named
# as Kathara names them (vecK -> ethK, the last one -> kmgmt), the sysctls
# and the ulimits of the device, sshd for /kathara-uml/ushell, then a
# status file the container can read.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

STATUS=/var/tmp/uml-status
BRIDGED_CONF=/var/lib/kathara-uml/bridged.conf
# The management link, as in start and ushell.
MGMT_UML=169.254.77.2
SYSCTLS=/kathara-uml/sysctl.conf
SYSCTL_LOG=/var/log/uml-sysctl.log
ULIMITS=/kathara-uml/ulimit.conf
ULIMIT_LOG=/var/log/uml-ulimit.log

# The filesystems that Docker mounts in a container, with its options,
# from the kernel of the UML; /dev with the devices of that kernel. The
# rest of / is the container's own, through hostfs: /run, /etc/hostname,
# /shared, the volumes.
mount_filesystems()
{
	mount -t proc proc /proc
	mount -t sysfs sysfs /sys
	mount -t devtmpfs devtmpfs /dev 2>/dev/null
	mkdir -p /dev/pts /dev/shm /dev/mqueue
	mount -t devpts devpts /dev/pts
	mount -t tmpfs -o nosuid,nodev,noexec,size=65536k shm /dev/shm
	mount -t mqueue -o nosuid,nodev,noexec mqueue /dev/mqueue
	mount -t cgroup2 -o ro,nosuid,nodev,noexec cgroup /sys/fs/cgroup
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
	ip addr add "$MGMT_UML/30" dev kmgmt
	ip link set kmgmt up
}

# The address and the default route that Docker gave the bridged interface
# of the container, which start moved to BRIDGED_CONF, one key=value per
# line: set on the same interface of the UML.
configure_bridged()
{
	local key value ifname address gateway

	if [ ! -f "$BRIDGED_CONF" ]; then
		return
	fi
	while IFS='=' read -r key value; do
		case "$key" in
		ifname)
			ifname=$value
			;;
		address)
			address=$value
			;;
		gateway)
			gateway=$value
			;;
		esac
	done < "$BRIDGED_CONF"
	ip addr add "$address" dev "$ifname"
	ip route add default via "$gateway" dev "$ifname"
}

# The sysctls that Kathara gives a device container, which it writes in
# sysctl.conf, one name=value per line: applied to the UML kernel, after
# the interfaces have their names. The log lists the ones that the UML
# kernel refuses, for example because it lacks them.
apply_sysctls()
{
	local setting

	: > "$SYSCTL_LOG"
	if [ ! -f "$SYSCTLS" ]; then
		return
	fi
	while read -r setting; do
		if ! sysctl -q -w "$setting" 2>> "$SYSCTL_LOG"; then
			echo "not applied: $setting" >> "$SYSCTL_LOG"
		fi
	done < "$SYSCTLS"
}

# The ulimits of the device, which Kathara writes in ulimit.conf, one
# "name soft hard" per line: set on init with prlimit before it starts
# sshd, so that every process of the UML inherits them. The log lists the
# ones that cannot be set.
apply_ulimits()
{
	local name soft hard

	: > "$ULIMIT_LOG"
	if [ ! -f "$ULIMITS" ]; then
		return
	fi
	while read -r name soft hard; do
		if ! prlimit --pid $$ "--$name=$soft:$hard" 2>> "$ULIMIT_LOG"; then
			echo "not applied: $name $soft $hard" >> "$ULIMIT_LOG"
		fi
	done < "$ULIMITS"
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
configure_bridged
apply_sysctls
apply_ulimits

# Without PAM, as docker exec runs a command in a container: pam_limits of
# the session would reset the ulimits that init has set.
mkdir -p /run/sshd
/usr/sbin/sshd -o ListenAddress="$MGMT_UML" -o UseDNS=no -o PermitRootLogin=prohibit-password -o UsePAM=no

while true; do
	write_status
	sleep 2
done
"""

USHELL = r"""#!/bin/bash
# Shell of a Kathara UML device. Kathara runs the startup as `shell -c
# CMDS`, exec as `shell -c CMD` and connect as `shell`: all go into the
# UML through one ssh on the management veth, with the environment of the
# device exported first, and run there in the shell of the device, as in
# a container.
# While the UML boots, ssh retries only the TCP connect, once a second
# (ConnectionAttempts), so a command never runs twice. ConnectTimeout
# bounds each connect and the ssh banner exchange.

KEY=/var/lib/kathara-uml/id
# The management link, as in start and init.
MGMT_UML=169.254.77.2
ENVIRONMENT=/kathara-uml/environment
SHELL_FILE=/kathara-uml/shell
SSH=(ssh -q -i "$KEY"
	-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR
	-o ConnectionAttempts=120 -o ConnectTimeout=10
	"root@$MGMT_UML")

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

# The environment of the device, which Kathara writes in environment, one
# NAME=VALUE per line, as export commands for the shell of the UML: there
# ssh starts a new session, without it.
exports()
{
	local setting

	if [ ! -f "$ENVIRONMENT" ]; then
		return
	fi
	while IFS= read -r setting; do
		printf 'export %q; ' "$setting"
	done < "$ENVIRONMENT"
}

# The shell of the device, which Kathara writes in shell: the shell option
# of the device, else the device_shell setting.
device_shell()
{
	if [ -s "$SHELL_FILE" ]; then
		cat "$SHELL_FILE"
	else
		echo /bin/bash
	fi
}

wait_for_key
shell=$(device_shell)
if [ "$1" = -c ]; then
	shift
	exec "${SSH[@]}" -T -- "$(exports)exec $shell -c $(printf %q "$*")"
fi
exec "${SSH[@]}" -t -- "$(exports)exec $shell"
"""


def uml_sysctl_conf(sysctls: Dict[str, Union[int, str]]) -> str:
    """Return the sysctls of a device in the format that init reads: one name=value per line, sorted by name.

    Args:
        sysctls (Dict[str, Union[int, str]]): The sysctls of the device.

    Returns:
        str: The content of sysctl.conf.
    """
    return "".join(f"{name}={value}\n" for name, value in sorted(sysctls.items()))


def uml_environment(envs: Dict[str, str]) -> str:
    """Return the environment variables of a device in the format that ushell reads: one NAME=VALUE per line,
    sorted by name.

    Args:
        envs (Dict[str, str]): The environment variables of the device.

    Returns:
        str: The content of environment.
    """
    return "".join(f"{name}={value}\n" for name, value in sorted(envs.items()))


class UmlSettings(object):
    """The settings of a device that the helpers apply inside its UML kernel, as Docker applies them to a container.

    Attributes:
        sysctls (Dict[str, Union[int, str]]): The sysctls that Kathara gives the container, the interface ones
            included.
        envs (Dict[str, str]): The environment variables of the device.
        shell (str): The shell of the device: its shell option, else the device_shell setting.
        ulimits (Dict[str, Dict[str, int]]): The ulimits of the device, as soft and hard limit by name.
    """
    __slots__ = ['sysctls', 'envs', 'shell', 'ulimits']

    def __init__(self, sysctls: Dict[str, Union[int, str]], envs: Dict[str, str], shell: str,
                 ulimits: Dict[str, Dict[str, int]]) -> None:
        self.sysctls: Dict[str, Union[int, str]] = sysctls
        self.envs: Dict[str, str] = envs
        self.shell: str = shell
        self.ulimits: Dict[str, Dict[str, int]] = ulimits

    def files(self) -> Dict[str, str]:
        """Return the files that carry the settings into the container, by their name in /kathara-uml.

        Returns:
            Dict[str, str]: The content of each file.
        """
        return {"sysctl.conf": uml_sysctl_conf(self.sysctls), "environment": uml_environment(self.envs),
                "shell": f"{self.shell}\n", "ulimit.conf": uml_ulimit_conf(self.ulimits)}


def uml_ulimit_conf(ulimits: Dict[str, Dict[str, int]]) -> str:
    """Return the ulimits of a device in the format that init reads: one "name soft hard" per line, sorted by name,
    with -1 written as unlimited.

    Args:
        ulimits (Dict[str, Dict[str, int]]): The ulimits of the device, as soft and hard limit by name.

    Returns:
        str: The content of ulimit.conf.
    """
    def limit(value: int) -> str:
        return "unlimited" if value == -1 else str(value)

    return "".join(f"{name} {limit(values['soft'])} {limit(values['hard'])}\n"
                   for name, values in sorted(ulimits.items()))


def uml_helpers_tar(settings: UmlSettings) -> bytes:
    """Return a tar archive with the UML helpers in kathara-uml/, executable, and the files of the settings of the
    device, to be extracted in `/`.

    Args:
        settings (UmlSettings): The settings that the helpers apply inside the UML kernel.

    Returns:
        bytes: The tar archive.
    """
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tar:
        directory = tarfile.TarInfo("kathara-uml")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        tar.addfile(directory)
        files = [("start", START, 0o755), ("init", INIT, 0o755), ("ushell", USHELL, 0o755)]
        files += [(name, content, 0o644) for name, content in settings.files().items()]
        for name, content, mode in files:
            encoded = content.encode("utf-8")
            info = tarfile.TarInfo("kathara-uml/" + name)
            info.size = len(encoded)
            info.mode = mode
            tar.addfile(info, io.BytesIO(encoded))
    return data.getvalue()
