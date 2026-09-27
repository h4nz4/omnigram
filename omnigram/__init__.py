from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("omnigram")  # from pyproject.toml; packaged builds carry the metadata (see build.py)
except PackageNotFoundError:
    __version__ = "dev"
