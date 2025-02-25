from __future__ import annotations

import io
import itertools
import json
import os
import shutil
import warnings
import zipfile
from functools import cached_property, lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from installer._core import _determine_scheme, _process_WHEEL_file, install
from installer.destinations import SchemeDictionaryDestination
from installer.exceptions import InvalidWheelSource
from installer.records import RecordEntry
from installer.sources import WheelFile as _WheelFile
from installer.sources import _WheelFileValidationError

from pdm.exceptions import PDMWarning
from pdm.installers.packages import CachedPackage
from pdm.termui import logger

if TYPE_CHECKING:
    from typing import Any, BinaryIO, Callable, Iterable, Protocol

    from installer.destinations import Scheme
    from installer.sources import WheelContentElement

    from pdm.environments import BaseEnvironment

    class LinkMethod(Protocol):
        def __call__(self, src: str | Path, dst: str | Path, target_is_directory: bool = False) -> None:
            ...


@lru_cache
def _is_python_package(root: str | Path) -> bool:
    for child in Path(root).iterdir():
        if (
            child.is_file()
            and child.suffix in (".py", ".pyc", ".pyo", ".pyd", ".pyi")
            or child.is_dir()
            and _is_python_package(child)
        ):
            return True
    return False


def _get_dist_name(wheel_path: str) -> str:
    from packaging.utils import parse_wheel_filename

    return parse_wheel_filename(os.path.basename(wheel_path))[0]


_namespace_package_lines = frozenset(
    [
        # pkg_resources style
        "__import__('pkg_resources').declare_namespace(__name__)",
        "pkg_resources.declare_namespace(__name__)",
        "declare_namespace(__name__)",
        # pkgutil style
        "__path__ = __import__('pkgutil').extend_path(__path__, __name__)",
        "__path__ = pkgutil.extend_path(__path__, __name__)",
        "__path__ = extend_path(__path__, __name__)",
    ]
)
_namespace_package_lines = _namespace_package_lines.union(line.replace("'", '"') for line in _namespace_package_lines)


@lru_cache
def _is_namespace_package(root: str) -> bool:
    if not _is_python_package(root):
        return False
    if not os.path.exists(os.path.join(root, "__init__.py")):  # PEP 420 style
        return True
    with Path(root, "__init__.py").open(encoding="utf-8") as f:
        init_py_lines = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
    return not _namespace_package_lines.isdisjoint(init_py_lines)


def _create_links_recursively(
    source: str, destination: str, link_method: LinkMethod, link_individual: bool
) -> Iterable[str]:
    """Create symlinks recursively from source to destination.
    package(if not individual)  <-- link
        __init__.py
    namespace_package  <-- mkdir
        foo.py  <-- link
        bar.py  <-- link
    """
    is_top = True
    for root, dirs, files in os.walk(source):
        bn = os.path.basename(root)
        if bn == "__pycache__" or bn.endswith(".dist-info"):
            dirs[:] = []
            continue
        relpath = os.path.relpath(root, source)
        destination_root = os.path.join(destination, relpath)
        if is_top:
            is_top = False
        elif not _is_namespace_package(root) and not link_individual:
            # A package, create link for the parent dir and don't proceed
            # for child directories
            if os.path.exists(destination_root):
                if not os.path.islink(destination_root):
                    warnings.warn(f"Overwriting existing package: {destination_root}", PDMWarning, stacklevel=2)
                if os.path.isdir(destination_root) and not os.path.islink(destination_root):
                    shutil.rmtree(destination_root)
                else:
                    os.remove(destination_root)
            link_method(root, destination_root, True)
            yield relpath
            dirs[:] = []
            continue
        # Otherwise, the directory is likely a namespace package,
        # mkdir and create links for all files inside.
        if not os.path.exists(destination_root):
            os.makedirs(destination_root)
        for f in files:
            if f.endswith(".pyc"):
                continue
            source_path = os.path.join(root, f)
            destination_path = os.path.join(destination_root, f)
            if os.path.exists(destination_path):
                os.remove(destination_path)
            link_method(source_path, destination_path, False)
            yield os.path.join(relpath, f)


class WheelFile(_WheelFile):
    def __init__(self, f: zipfile.ZipFile) -> None:
        super().__init__(f)
        self.exclude: Callable[[WheelFile, WheelContentElement], bool] | None = None
        self.additional_contents: Iterable[WheelContentElement] = []

    @cached_property
    def dist_info_dir(self) -> str:
        namelist = self._zipfile.namelist()
        try:
            return next(name.split("/")[0] for name in namelist if name.split("/")[0].endswith(".dist-info"))
        except StopIteration:  # pragma: no cover
            canonical_name = super().dist_info_dir
            raise InvalidWheelSource(f"The wheel doesn't contain metadata {canonical_name!r}") from None

    def get_contents(self) -> Iterator[WheelContentElement]:
        for element in super().get_contents():
            if self.exclude and self.exclude(self, element):
                continue
            yield element
        yield from self.additional_contents


class InstallDestination(SchemeDictionaryDestination):
    def __init__(
        self,
        *args: Any,
        link_to: str | None = None,
        link_method: LinkMethod | None = None,
        link_individual: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.link_to = link_to
        self.link_method = link_method
        self.link_individual = link_individual

    def write_to_fs(self, scheme: Scheme, path: str | Path, stream: BinaryIO, is_executable: bool) -> RecordEntry:
        target_path = os.path.join(self.scheme_dict[scheme], path)
        if os.path.exists(target_path):
            os.unlink(target_path)
        return super().write_to_fs(scheme, path, stream, is_executable)

    def finalize_installation(
        self,
        scheme: Scheme,
        record_file_path: str | Path,
        records: Iterable[tuple[Scheme, RecordEntry]],
    ) -> None:
        if self.link_method is not None:
            # Create symlinks to the cached location
            def _link_files() -> Iterator[tuple[Scheme, RecordEntry]]:
                assert self.link_method is not None
                assert self.link_to is not None
                for relpath in _create_links_recursively(
                    self.link_to, self.scheme_dict[scheme], self.link_method, self.link_individual
                ):
                    yield (scheme, RecordEntry(relpath.replace("\\", "/"), None, None))

            records = itertools.chain(records, _link_files())
        return super().finalize_installation(scheme, record_file_path, records)


def install_wheel(wheel: str, environment: BaseEnvironment, direct_url: dict[str, Any] | None = None) -> str:
    """Install a normal wheel file into the environment."""
    additional_metadata = None
    if direct_url is not None:
        additional_metadata = {"direct_url.json": json.dumps(direct_url, indent=2).encode()}
    destination = InstallDestination(
        scheme_dict=environment.get_paths(_get_dist_name(wheel)),
        interpreter=str(environment.interpreter.executable),
        script_kind=_get_kind(environment),
    )
    return _install_wheel(wheel=wheel, destination=destination, additional_metadata=additional_metadata)


def _get_link_method_and_individual(cache_method: str) -> tuple[LinkMethod | None, bool]:
    from pdm import utils

    def _hardlink(src: str | Path, dst: str | Path, target_is_directory: bool = False) -> None:
        os.link(src, dst)

    if "symlink" in cache_method and utils.fs_supports_link_method("symlink"):
        return os.symlink, "individual" in cache_method

    if "link" in cache_method and utils.fs_supports_link_method("link"):
        return _hardlink, True
    return None, False


def install_wheel_with_cache(wheel: str, environment: BaseEnvironment, direct_url: dict[str, Any] | None = None) -> str:
    """Only create .pth files referring to the cached package.
    If the cache doesn't exist, create one.
    """
    wheel_stem = Path(wheel).stem
    cache_path = environment.project.cache("packages") / wheel_stem
    package_cache = CachedPackage(cache_path)
    interpreter = str(environment.interpreter.executable)
    script_kind = _get_kind(environment)
    # the cache_method can be any of "symlink", "hardlink", "symlink_individual" and "pth"
    cache_method: str = environment.project.config["install.cache_method"]
    link_method, link_individual = _get_link_method_and_individual(cache_method)
    if not cache_path.is_dir():
        logger.info("Installing wheel into cached location %s", cache_path)
        cache_path.mkdir(exist_ok=True)
        destination = InstallDestination(
            scheme_dict=package_cache.scheme(),
            interpreter=interpreter,
            script_kind=script_kind,
        )
        _install_wheel(wheel=wheel, destination=destination)

    additional_metadata = {"REFER_TO": package_cache.path.as_posix().encode()}

    if direct_url is not None:
        additional_metadata["direct_url.json"] = json.dumps(direct_url, indent=2).encode()

    def skip_files(source: WheelFile, element: WheelContentElement) -> bool:
        root_scheme = _process_WHEEL_file(source)
        scheme, path = _determine_scheme(element[0][0], source, root_scheme)
        return not (
            scheme not in ("purelib", "platlib")
            or path.split("/")[0].endswith(".dist-info")
            # We need to skip the *-nspkg.pth files generated by setuptools'
            # namespace_packages merchanism. See issue#623 for details
            or link_method is None
            and path.endswith(".pth")
            and not path.endswith("-nspkg.pth")
        )

    additional_contents: list[WheelContentElement] = []
    lib_path = package_cache.scheme()["purelib"]
    if link_method is None:
        # HACK: Prefix with aaa_ to make it processed as early as possible
        filename = "aaa_" + wheel_stem.split("-")[0] + ".pth"
        # use site.addsitedir() rather than a plain path to properly process .pth files
        stream = io.BytesIO(f"import site;site.addsitedir({lib_path!r})\n".encode())
        additional_contents.append(((filename, "", str(len(stream.getvalue()))), stream, False))

    destination = InstallDestination(
        scheme_dict=environment.get_paths(_get_dist_name(wheel)),
        interpreter=interpreter,
        script_kind=script_kind,
        link_to=lib_path,
        link_method=link_method,
        link_individual=link_individual,
    )

    dist_info_dir = _install_wheel(
        wheel=wheel,
        destination=destination,
        excludes=skip_files,
        additional_contents=additional_contents,
        additional_metadata=additional_metadata,
    )
    package_cache.add_referrer(dist_info_dir)
    return dist_info_dir


def _install_wheel(
    wheel: str,
    destination: InstallDestination,
    excludes: Callable[[WheelFile, WheelContentElement], bool] | None = None,
    additional_contents: Iterable[WheelContentElement] | None = None,
    additional_metadata: dict[str, bytes] | None = None,
) -> str:
    """A lower level installation method that is copied from installer
    but is controlled by extra parameters.

    Return the .dist-info path
    """
    with WheelFile.open(wheel) as source:
        try:
            source.validate_record()
        except _WheelFileValidationError as e:
            formatted_issues = "\n".join(e.issues)
            warning = (
                f"Validation of the RECORD file of {wheel} failed."
                " Please report to the maintainers of that package so they can fix"
                f" their build process. Details:\n{formatted_issues}\n"
            )
            warnings.warn(warning, PDMWarning, stacklevel=2)
        root_scheme = _process_WHEEL_file(source)
        source.exclude = excludes
        if additional_contents:
            source.additional_contents = additional_contents
        install(source, destination, additional_metadata=additional_metadata or {})
    return os.path.join(destination.scheme_dict[root_scheme], source.dist_info_dir)


def _get_kind(environment: BaseEnvironment) -> str:
    if os.name != "nt":
        return "posix"
    is_32bit = environment.interpreter.is_32bit
    # TODO: support win arm64
    if is_32bit:
        return "win-ia32"
    else:
        return "win-amd64"
