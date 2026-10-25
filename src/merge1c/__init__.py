"""Public API for integrating 1C Designer sources."""

from .bslmerge import inspect_bsl, merge_bsl
from .config import Config, ConfigError, Tool, load_config
from .engine import DirectoryResult, MergeError, merge_directories, validate_directory
from .gitrepo import GitError
from .models import Change, Issue, MergeResult
from .session import AlreadyIntegrated, Session, SessionError, prepare_directories, prepare_repository
from .workspace import StorageError
from .xmlmerge import inspect_xml, merge_xml

__version__ = "0.1.2"

__all__ = [
    "AlreadyIntegrated", "Change", "Config", "ConfigError", "DirectoryResult", "GitError", "Issue", "MergeError", "MergeResult",
    "Session", "SessionError", "StorageError", "Tool", "inspect_bsl", "inspect_xml", "load_config", "merge_bsl",
    "merge_directories", "merge_xml", "prepare_directories", "prepare_repository", "validate_directory",
]
