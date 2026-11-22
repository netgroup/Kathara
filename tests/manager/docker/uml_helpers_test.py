import io
import sys
import tarfile

sys.path.insert(0, './')

from src.Kathara.manager.docker.UmlHelpers import UmlSettings, uml_helpers_tar, uml_sysctl_conf, START, INIT, \
    USHELL


def test_uml_helpers_tar():
    tar = tarfile.open(fileobj=io.BytesIO(uml_helpers_tar(UmlSettings(sysctls={'net.ipv4.ip_forward': 1}))))

    assert tar.getnames() == ['kathara-uml', 'kathara-uml/start', 'kathara-uml/init', 'kathara-uml/ushell',
                              'kathara-uml/sysctl.conf']
    modes = {member.name: member.mode for member in tar.getmembers()}
    assert modes == {'kathara-uml': 0o755, 'kathara-uml/start': 0o755, 'kathara-uml/init': 0o755,
                     'kathara-uml/ushell': 0o755, 'kathara-uml/sysctl.conf': 0o644}
    assert tar.extractfile('kathara-uml/start').read().decode('utf-8') == START
    assert tar.extractfile('kathara-uml/init').read().decode('utf-8') == INIT
    assert tar.extractfile('kathara-uml/ushell').read().decode('utf-8') == USHELL
    assert tar.extractfile('kathara-uml/sysctl.conf').read().decode('utf-8') == 'net.ipv4.ip_forward=1\n'


def test_uml_sysctl_conf():
    assert uml_sysctl_conf({'net.ipv6.conf.all.disable_ipv6': 1, 'net.ipv4.conf.eth1.rp_filter': 2}) == \
           'net.ipv4.conf.eth1.rp_filter=2\nnet.ipv6.conf.all.disable_ipv6=1\n'
    assert uml_sysctl_conf({}) == ''


def test_uml_helpers_scripts():
    for script in (START, INIT, USHELL):
        assert script.startswith('#!/bin/bash\n')
    assert 'seccomp=${KATHARA_UML_SECCOMP:-auto}' in START
    assert 'init=/kathara-uml/init' in START
    assert '/run/kathara-uml.ready' in START
    assert '-o ConnectionAttempts=120' in USHELL
    assert ',mac=$mac' in START
    assert 'apply_sysctls' in INIT
