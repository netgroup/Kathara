import io
import struct
import sys
import tarfile

sys.path.insert(0, './')

import pytest

from src.Kathara.manager.docker.UmlHelpers import UmlSettings, uml_helpers_tar, uml_sysctl_conf, uml_environment, \
    uml_ulimit_conf, uml_kernel_error, START, INIT, USHELL, UML_BOOTED, UML_CONSOLE


def test_uml_helpers_tar():
    settings = UmlSettings(sysctls={'net.ipv4.ip_forward': 1}, envs={'GREETING': 'hello world'}, shell='/bin/sh',
                           ulimits={'nofile': {'soft': 1024, 'hard': 2048}})
    tar = tarfile.open(fileobj=io.BytesIO(uml_helpers_tar(settings)))

    assert tar.getnames() == ['kathara-uml', 'kathara-uml/start', 'kathara-uml/init', 'kathara-uml/ushell',
                              'kathara-uml/sysctl.conf', 'kathara-uml/environment', 'kathara-uml/shell',
                              'kathara-uml/ulimit.conf']
    modes = {member.name: member.mode for member in tar.getmembers()}
    assert modes == {'kathara-uml': 0o755, 'kathara-uml/start': 0o755, 'kathara-uml/init': 0o755,
                     'kathara-uml/ushell': 0o755, 'kathara-uml/sysctl.conf': 0o644,
                     'kathara-uml/environment': 0o644, 'kathara-uml/shell': 0o644, 'kathara-uml/ulimit.conf': 0o644}
    assert tar.extractfile('kathara-uml/start').read().decode('utf-8') == START
    assert tar.extractfile('kathara-uml/init').read().decode('utf-8') == INIT
    assert tar.extractfile('kathara-uml/ushell').read().decode('utf-8') == USHELL
    assert tar.extractfile('kathara-uml/sysctl.conf').read().decode('utf-8') == 'net.ipv4.ip_forward=1\n'
    assert tar.extractfile('kathara-uml/environment').read().decode('utf-8') == 'GREETING=hello world\n'
    assert tar.extractfile('kathara-uml/shell').read().decode('utf-8') == '/bin/sh\n'
    assert tar.extractfile('kathara-uml/ulimit.conf').read().decode('utf-8') == 'nofile 1024 2048\n'


def test_uml_helpers_management_link():
    # start puts the container end, init and ushell the UML end, of the same /30.
    assert 'MGMT_CONTAINER=169.254.77.1/30\n' in START
    assert 'MGMT_UML=169.254.77.2\n' in INIT
    assert 'MGMT_UML=169.254.77.2\n' in USHELL


def test_uml_helpers_booted():
    # init touches the file that DockerMachine.start() waits for.
    assert f'\ntouch {UML_BOOTED}\n' in INIT


def test_uml_helpers_console():
    # start writes the console where DockerMachine.start() reads it.
    assert f'> {UML_CONSOLE} 2>&1\n' in START


def test_uml_sysctl_conf():
    assert uml_sysctl_conf({'net.ipv6.conf.all.disable_ipv6': 1, 'net.ipv4.conf.eth1.rp_filter': 2}) == \
           'net.ipv4.conf.eth1.rp_filter=2\nnet.ipv6.conf.all.disable_ipv6=1\n'
    assert uml_sysctl_conf({}) == ''


def test_uml_environment():
    assert uml_environment({'B': 'two words', 'A': '1'}) == 'A=1\nB=two words\n'
    assert uml_environment({}) == ''


def test_uml_ulimit_conf():
    assert uml_ulimit_conf({'nofile': {'soft': 1024, 'hard': 2048}, 'memlock': {'soft': -1, 'hard': -1}}) == \
           'memlock unlimited unlimited\nnofile 1024 2048\n'
    assert uml_ulimit_conf({'core': {'soft': 0, 'hard': -1}}) == 'core 0 unlimited\n'
    assert uml_ulimit_conf({}) == ''


def test_uml_helpers_scripts():
    for script in (START, INIT, USHELL):
        assert script.startswith('#!/bin/bash\n')
    assert 'seccomp=${KATHARA_UML_SECCOMP:-auto}' in START
    assert 'init=/kathara-uml/init' in START
    assert '/run/kathara-uml.ready' in START
    assert '-o ConnectionAttempts=120' in USHELL
    assert ',mac=$mac' in START
    assert 'umount /etc/resolv.conf' in START and 'umount /etc/hosts' in START
    assert 'take_bridged_config "$bridged"' in START and 'configure_bridged' in INIT
    assert 'apply_sysctls' in INIT


def elf(elf_class: int, machine: int, sections: list, data: int = 1) -> bytes:
    """Return an ELF file with the class, machine, data encoding and sections given, empty but for their names."""
    if elf_class == 2:
        header_format, section_format = "<16sHHIQQQIHHHHHH", "<IIQQQQIIQQ"
    else:
        header_format, section_format = "<16sHHIIIIIHHHHHH", "<IIIIIIIIII"
    names = b"\0"
    offsets = []
    for name in sections + [b".shstrtab"]:
        offsets.append(len(names))
        names += name + b"\0"
    header_size = struct.calcsize(header_format)
    section_size = struct.calcsize(section_format)
    ident = b"\x7fELF" + bytes([elf_class, data, 1]) + bytes(9)
    header = struct.pack(header_format, ident, 2, machine, 1, 0, 0, header_size + len(names), 0, header_size, 0, 0,
                         section_size, len(offsets) + 1, len(offsets))
    table = bytes(section_size)
    for offset in offsets[:-1]:
        table += struct.pack(section_format, offset, 1, 0, 0, 0, 0, 0, 0, 0, 0)
    table += struct.pack(section_format, offsets[-1], 3, 0, 0, header_size, len(names), 0, 0, 1, 0)
    return header + names + table


UML_AMD64 = elf(2, 62, [b".text", b".uml.setup.init"])
UML_386 = elf(1, 3, [b".text", b".uml.setup.init"])
VMLINUX = elf(2, 62, [b".text", b".data"])
BZIMAGE = b"MZ" + bytes(0x200) + b"HdrS" + bytes(16)


@pytest.mark.parametrize("kernel,host_arch", [(UML_AMD64, "amd64"), (UML_386, "386")])
def test_uml_kernel_error_none(tmp_path, kernel, host_arch):
    path = tmp_path / "linux"
    path.write_bytes(kernel)

    assert uml_kernel_error(str(path), host_arch) is None


@pytest.mark.parametrize("kernel,host_arch,error", [
    (UML_AMD64, "arm64", "UML runs only on x86 hosts, this host is `arm64`"),
    (UML_AMD64, "386", "it is a UML kernel for `amd64`, this host is `386`"),
    (UML_386, "amd64", "it is a UML kernel for `386`, this host is `amd64`"),
    (elf(2, 183, [b".uml.setup.init"]), "amd64", "it is a UML kernel for `ELF machine 183`, this host is `amd64`"),
    (VMLINUX, "amd64", "it is not a UML kernel: build the kernel with ARCH=um"),
    (BZIMAGE, "amd64", "it is a kernel boot image (bzImage), not a UML kernel: build the kernel with ARCH=um"),
    (b"#!/bin/sh\n", "amd64", "it is not a UML kernel: build the kernel with ARCH=um"),
    (b"", "amd64", "it is not a UML kernel: build the kernel with ARCH=um"),
    (UML_AMD64[:100], "amd64", "it is not a UML kernel: build the kernel with ARCH=um"),
    (elf(2, 62, [b".uml.setup.init"], data=2), "amd64", "it is not a UML kernel: build the kernel with ARCH=um"),
])
def test_uml_kernel_error(tmp_path, kernel, host_arch, error):
    path = tmp_path / "linux"
    path.write_bytes(kernel)

    assert uml_kernel_error(str(path), host_arch) == error
