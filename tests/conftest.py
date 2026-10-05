"""Unit tests replace only the cloud image-build boundary."""

from unittest.mock import patch

with patch("images.build"):
    import app  # noqa: F401
