"""Store identity, runtime boundaries and the platform-independent MSIX layout."""
import ctypes
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock
import xml.etree.ElementTree as ET

import pytest

import app_info
import autostart
from installer import make_msix
import updater


def generate_layout_cli(source, layout, *identity):
    # test_core replaces Pillow with stand-ins; the packager needs a clean interpreter.
    command = [sys.executable, make_msix.__file__, "--layout-only", "--source", str(source),
               "--layout", str(layout), "--version", "2.1.1"]
    for flag, value in zip(("--identity-name", "--publisher", "--publisher-display-name"), identity):
        command.extend((flag, value))
    subprocess.run(command, check=True, capture_output=True, text=True)
    return layout


@pytest.fixture
def identity_cache():
    app_info.is_store_package.cache_clear()
    yield
    app_info.is_store_package.cache_clear()


def test_identity_non_windows(identity_cache):
    with mock.patch.object(app_info.sys, "platform", "darwin"), \
            mock.patch.object(ctypes, "WinDLL", create=True) as dll:
        assert app_info.is_store_package() is False
        dll.assert_not_called()


@pytest.mark.parametrize("result,expected", [(0, True), (15700, False), (5, False)])
def test_identity_result_and_cache(identity_cache, result, expected):
    query = mock.Mock(return_value=result)
    with mock.patch.object(app_info.sys, "platform", "win32"), \
            mock.patch.object(ctypes, "WinDLL", return_value=SimpleNamespace(GetCurrentPackageFullName=query), create=True):
        assert app_info.is_store_package() is expected
        assert app_info.is_store_package() is expected
        query.assert_called_once()


@pytest.mark.parametrize("second_result,expected", [(0, True), (15700, False), (5, False)])
def test_identity_buffer_query(identity_cache, second_result, expected):
    def query(length, buffer):
        if buffer is None:
            length._obj.value = 32
            return 122
        return second_result

    with mock.patch.object(app_info.sys, "platform", "win32"), \
            mock.patch.object(ctypes, "WinDLL", return_value=SimpleNamespace(GetCurrentPackageFullName=mock.Mock(side_effect=query)), create=True):
        assert app_info.is_store_package() is expected


def test_identity_api_unavailable(identity_cache):
    with mock.patch.object(app_info.sys, "platform", "win32"), \
            mock.patch.object(ctypes, "WinDLL", side_effect=OSError("unavailable"), create=True):
        assert app_info.is_store_package() is False


def test_identity_empty_buffer(identity_cache):
    with mock.patch.object(app_info.sys, "platform", "win32"), \
            mock.patch.object(ctypes, "WinDLL", return_value=SimpleNamespace(GetCurrentPackageFullName=mock.Mock(return_value=122)), create=True):
        assert app_info.is_store_package() is False


def test_store_blocks_updates(monkeypatch):
    monkeypatch.setattr(updater, "is_store_package", lambda: True)
    forbidden = mock.Mock(side_effect=AssertionError("Store must not access external updater"))
    monkeypatch.setattr(updater.os, "listdir", forbidden)
    assert updater.check_latest(get=forbidden) == (updater.STORE, "")
    assert not updater.should_prompt({}, "v99.0.0")
    assert not updater.can_self_install()
    assert updater.status_line(updater.AVAILABLE, "v99.0.0")[1] == updater.STORE_MESSAGE
    for operation, args, kwargs in (
        (updater.download, ("v99.0.0",), {"urlopen": forbidden}),
        (updater.cached_download, ("v99.0.0",), {"urlopen": forbidden}),
        (updater.install, ("v99.0.0",), {"urlopen": forbidden}),
        (updater.launch, ("setup.exe",), {"startfile": forbidden}),
        (updater.launch_installer, ("setup.exe",), {"start": forbidden}),
    ):
        assert operation(*args, **kwargs) == (False, updater.STORE_MESSAGE)
    assert updater.spawn_relauncher("app.exe", "log", popen=forbidden) is None
    forbidden.assert_not_called()


def test_store_autostart_does_not_read_or_write_registry_or_marker(monkeypatch):
    monkeypatch.setattr(autostart, "is_store_package", lambda: True)
    forbidden = mock.Mock(side_effect=AssertionError("Store must not manage Run keys"))
    monkeypatch.setattr(autostart, "_winreg", forbidden)
    monkeypatch.setattr(autostart, "_take_install_default", forbidden)
    cfg = {"launch_at_login": True}
    assert not autostart.supported()
    assert not autostart.reconcile(cfg)
    assert not autostart.set_enabled(True)
    assert not autostart.set_enabled(False)
    assert cfg == {"launch_at_login": True}
    forbidden.assert_not_called()


def test_store_controller_ignores_cached_updates(monkeypatch):
    try:
        from tests import test_core  # noqa: F401
    except ImportError:
        import test_core  # noqa: F401
    import main

    monkeypatch.setattr(main, "is_store_package", lambda: True)
    app = SimpleNamespace()
    for method, args in (("_background_check_updates", ()), ("note_update_check", (updater.AVAILABLE, "v99.0.0")),
                         ("_prepare_update_on_quit", ("v99.0.0",)), ("_run_update_on_quit", ()),
                         ("_after_restart_checks", ())):
        assert getattr(main.AppController, method)(app, *args) is None
    done = mock.Mock()
    assert main.AppController.install_update(app, "v99.0.0", on_done=done) is False
    done.assert_called_once_with(False, updater.STORE_MESSAGE)
    assert main._running_update_install({"update_attempt": {"to": "v99.0.0"}}) is None


def test_layout(tmp_path):
    source = tmp_path / "dist"
    source.mkdir()
    (source / "TranscribeApp.exe").write_bytes(b"test executable")
    (source / "_internal").mkdir()
    (source / "_internal" / "resource.txt").write_text("resource")
    layout = generate_layout_cli(source, tmp_path / "layout", "Example.Gusan",
                                 'CN=Example & Co', 'Example & "Co"')
    manifest = (layout / "AppxManifest.xml").read_text()
    assert "{{" not in manifest
    root = ET.fromstring(manifest)
    ns = {"f": "http://schemas.microsoft.com/appx/manifest/foundation/windows10",
          "d": "http://schemas.microsoft.com/appx/manifest/desktop/windows10",
          "u10": "http://schemas.microsoft.com/appx/manifest/uap/windows10/10",
          "r": "http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"}
    identity = root.find("f:Identity", ns)
    assert identity.attrib == {"Name": "Example.Gusan", "Publisher": "CN=Example & Co",
                               "Version": "2.1.1.0", "ProcessorArchitecture": "x64"}
    assert root.find("f:Properties/f:PublisherDisplayName", ns).text == 'Example & "Co"'
    assert root.find(".//d:StartupTask", ns).attrib["Enabled"] == "true"
    extension = root.find(".//d:Extension", ns)
    assert extension.attrib["Category"] == "windows.startupTask"
    assert extension.attrib["Executable"] == "TranscribeApp.exe"
    assert extension.attrib["{" + ns["u10"] + "}Parameters"] == "--background"
    assert root.find(".//r:Capability", ns).attrib["Name"] == "runFullTrust"
    assert root.find(".//f:DeviceCapability", ns).attrib["Name"] == "microphone"
    assert root.find(".//f:Capability", ns).attrib["Name"] == "internetClient"
    assert root.find(".//f:TargetDeviceFamily", ns).attrib["MinVersion"] == "10.0.17763.0"
    assert (layout / "_internal/resource.txt").read_text() == "resource"
    for name, size in (("Square44x44Logo", (44, 44)), ("Square150x150Logo", (150, 150)),
                       ("Wide310x150Logo", (310, 150)), ("StoreLogo", (50, 50)),
                       ("Square44x44Logo.targetsize-44_altform-unplated", (44, 44))):
        png = (layout / "Assets" / (name + ".png")).read_bytes()
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", png[16:24]) == size
        assert b"IDAT" in png


@pytest.mark.parametrize("version", ["", "1.2", "v1.2.3", "1.2.3.0", "0.1.0", "1.65536.0"])
def test_invalid_package_version(version):
    with pytest.raises(ValueError):
        make_msix.package_version(version)


def test_layout_missing_exe(tmp_path):
    with pytest.raises(ValueError, match="TranscribeApp.exe"):
        make_msix.generate_layout(tmp_path, tmp_path.parent / "layout", "1.2.3")


def test_layout_rejects_overlapping_paths(tmp_path):
    with pytest.raises(ValueError, match="separate"):
        make_msix.generate_layout(tmp_path, tmp_path / "layout", "1.2.3")


def test_layout_defaults_and_existing_output(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "TranscribeApp.exe").write_bytes(b"exe")
    layout = generate_layout_cli(source, tmp_path / "layout")
    root = ET.parse(layout / "AppxManifest.xml").getroot()
    assert root[0].attrib["Name"] == "AIBUBEN.Gusan"
    with pytest.raises(FileExistsError):
        make_msix.generate_layout(source, layout, "1.2.3")
