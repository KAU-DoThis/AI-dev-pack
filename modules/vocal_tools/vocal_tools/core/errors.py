class VocalToolsError(Exception):
    """Base exception for vocal_tools errors."""


class ModelNotFoundError(VocalToolsError):
    """Raised when a model id is not present in the catalog."""


class ModelAlreadyExistsError(VocalToolsError):
    """Raised when registering a model would overwrite another model."""


class ModelNotDownloadedError(VocalToolsError):
    """Raised when a required local model file is missing."""


class ModelVerificationError(VocalToolsError):
    """Raised when a model file fails size or checksum verification."""


class ModelResolveError(VocalToolsError):
    """Raised when a model reference cannot be resolved to usable model files."""


class ModelNotInstalledError(ModelResolveError, ModelNotDownloadedError):
    """Raised when a catalog model is known but its local files are not installed."""


class ModelPathNotFoundError(ModelResolveError):
    """Raised when a model reference is treated as a path but does not exist."""


class UnsupportedEngineError(VocalToolsError):
    """Raised when an engine is unavailable or not implemented yet."""


class InvalidOptionError(VocalToolsError):
    """Raised when request options do not match an engine schema."""


class InvalidManifestError(VocalToolsError):
    """Raised when a model manifest cannot be parsed or validated."""


class SeparationError(VocalToolsError):
    """Raised when separation fails after request validation."""


class TTSError(VocalToolsError):
    """Raised when speech synthesis or its runtime fails."""


class TTSDependencyError(TTSError):
    """Raised when a TTS backend or required output codec is unavailable."""
