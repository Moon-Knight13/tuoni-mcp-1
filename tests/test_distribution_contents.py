"""Release archives reject local artifacts and extraction-sensitive entries."""

from __future__ import annotations

import io
import stat
import tarfile
import zipfile

import pytest

from scripts.check_dist import check


def _archive(tmp_path, name, *, wheel=False, member_type=tarfile.REGTYPE, zip_type=None):
    path = tmp_path / ("test.whl" if wheel else "test.tar.gz")
    if wheel:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("test.dist-info/licenses/LICENSE", "MIT")
            member = zipfile.ZipInfo(name)
            if member_type == tarfile.SYMTYPE:
                zip_type = stat.S_IFLNK
            if zip_type is not None:
                member.create_system = 3
                member.external_attr = (zip_type | 0o777) << 16
            archive.writestr(member, "/etc/passwd")
    else:
        with tarfile.open(path, "w:gz") as archive:
            license = tarfile.TarInfo("test/LICENSE")
            license.size = 3
            archive.addfile(license, io.BytesIO(b"MIT"))
            member = tarfile.TarInfo(name)
            member.type = member_type
            if member_type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                member.linkname = "/etc/passwd"
            archive.addfile(member)
    return path


@pytest.mark.parametrize("wheel", [False, True])
@pytest.mark.parametrize("name", [
    "src/.git/config", "src/.venv/bin/python", "src/venv/bin/python", "src/__pycache__/module.pyc",
    "src/.pytest_cache/CACHEDIR.TAG", "src/.ruff_cache/data", "src/.mypy_cache/data", "src/.tox/data",
    "src/build/module.py", "src/dist/old.whl", "src/package.egg-info/PKG-INFO",
    "src/module.pyc", "src/module.pyo", "src/.coverage", "src/.coverage.host",
    "src/.env", "src/.env.production", "src/override.env", "src/private.key",
    "../outside", "/absolute", r"src\..\outside", "C:/outside", "C:outside", "src/module.py:secret",
])
def test_generated_files_secrets_and_unsafe_paths_are_rejected(tmp_path, name, wheel):
    with pytest.raises(ValueError):
        check(_archive(tmp_path, name, wheel=wheel))


@pytest.mark.parametrize("wheel", [False, True])
@pytest.mark.parametrize("name", [
    "src/package/module.py", "test.dist-info/METADATA", "scripts/env/default.env",
    "scripts/env/.env.example", "scripts/env/override.env.example",
])
def test_valid_source_and_example_configuration_are_permitted(tmp_path, name, wheel):
    check(_archive(tmp_path, name, wheel=wheel))


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_source_distribution_rejects_links_and_special_files(tmp_path, member_type):
    with pytest.raises(ValueError, match="links and special"):
        check(_archive(tmp_path, "src/module.py", member_type=member_type))


def test_wheel_rejects_unix_symlink_entries(tmp_path):
    with pytest.raises(ValueError, match="links"):
        check(_archive(tmp_path, "src/module.py", wheel=True, member_type=tarfile.SYMTYPE))


@pytest.mark.parametrize("zip_type", [stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK])
def test_wheel_rejects_unix_special_entries(tmp_path, zip_type):
    with pytest.raises(ValueError, match="links and special"):
        check(_archive(tmp_path, "src/module.py", wheel=True, zip_type=zip_type))


@pytest.mark.parametrize("name,zip_type", [("src/module.py", stat.S_IFREG), ("src/package/", stat.S_IFDIR)])
def test_wheel_allows_unix_regular_files_and_directories(tmp_path, name, zip_type):
    check(_archive(tmp_path, name, wheel=True, zip_type=zip_type))


@pytest.mark.parametrize("wheel", [False, True])
def test_a_license_directory_does_not_replace_the_license_file(tmp_path, wheel):
    path = tmp_path / ("test.whl" if wheel else "test.tar.gz")
    if wheel:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("test/LICENSE/", "")
    else:
        with tarfile.open(path, "w:gz") as archive:
            member = tarfile.TarInfo("test/LICENSE")
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
    with pytest.raises(ValueError, match="License is missing"):
        check(path)
