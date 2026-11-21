import io
import sys
import tarfile

sys.path.insert(0, './')

from src.Kathara.manager.docker.UmlHelpers import uml_helpers_tar, START, INIT, USHELL


def test_uml_helpers_tar():
    tar = tarfile.open(fileobj=io.BytesIO(uml_helpers_tar()))

    assert tar.getnames() == ['kathara-uml', 'kathara-uml/start', 'kathara-uml/init', 'kathara-uml/ushell']
    for member in tar.getmembers():
        assert member.mode == 0o755
    assert tar.extractfile('kathara-uml/start').read().decode('utf-8') == START
    assert tar.extractfile('kathara-uml/init').read().decode('utf-8') == INIT
    assert tar.extractfile('kathara-uml/ushell').read().decode('utf-8') == USHELL


def test_uml_helpers_scripts():
    for script in (START, INIT, USHELL):
        assert script.startswith('#!/bin/bash\n')
    assert 'seccomp=${KATHARA_UML_SECCOMP:-auto}' in START
    assert 'init=/kathara-uml/init' in START
    assert '/run/kathara-uml.ready' in START
    assert '-o ConnectionAttempts=120' in USHELL
