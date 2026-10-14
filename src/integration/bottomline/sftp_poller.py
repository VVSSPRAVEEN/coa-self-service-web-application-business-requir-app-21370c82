"""SFTP poller for reading Bottomline inbound files from the drop location."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SftpFileEntry:
    """Represents a file discovered on the SFTP server."""

    file_name: str
    content: bytes
    batch_id: str


class SftpClient(Protocol):
    """Protocol for SFTP client operations."""

    def list_files(self, remote_dir: str) -> List[str]: ...
    def read_file(self, remote_path: str) -> bytes: ...
    def delete_file(self, remote_path: str) -> None: ...


class SftpPoller:
    """Polls an SFTP drop location for new Bottomline inbound files.

    Driven by Mule scheduler integration. Discovers files, reads their
    content, and extracts the BatchID from the filename convention.
    """

    def __init__(
        self,
        client: SftpClient,
        remote_dir: str,
        file_pattern: str = "*.txt",
    ) -> None:
        self._client = client
        self._remote_dir = remote_dir
        self._file_pattern = file_pattern

    def poll(self) -> List[SftpFileEntry]:
        """Poll the SFTP directory for new files.

        Returns:
            List of discovered file entries ready for ingestion.
        """
        entries: List[SftpFileEntry] = []
        try:
            file_names = self._client.list_files(self._remote_dir)
        except Exception:
            logger.exception("Failed to list SFTP directory: %s", self._remote_dir)
            return entries

        for file_name in file_names:
            if not self._matches_pattern(file_name):
                continue

            batch_id = self._extract_batch_id(file_name)
            if not batch_id:
                logger.warning("Cannot extract BatchID from file: %s", file_name)
                continue

            try:
                remote_path = f"{self._remote_dir}/{file_name}"
                content = self._client.read_file(remote_path)
                entries.append(
                    SftpFileEntry(
                        file_name=file_name,
                        content=content,
                        batch_id=batch_id,
                    )
                )
            except Exception:
                logger.exception("Failed to read SFTP file: %s", file_name)

        return entries

    def _matches_pattern(self, file_name: str) -> bool:
        """Check if a file name matches the expected pattern."""
        if self._file_pattern == "*.txt":
            return file_name.endswith(".txt")
        if self._file_pattern == "*.dat":
            return file_name.endswith(".dat")
        return True

    @staticmethod
    def _extract_batch_id(file_name: str) -> Optional[str]:
        """Extract BatchID from Bottomline file naming convention.

        Expected format: BATCH_<batchid>_<sequence>.txt
        Falls back to using the full stem as batch_id.
        """
        stem = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
        parts = stem.split("_")
        if len(parts) >= 2 and parts[0].upper() == "BATCH":
            return parts[1]
        return stem if stem else None

    def acknowledge(self, file_name: str) -> None:
        """Delete a successfully processed file from the SFTP drop."""
        remote_path = f"{self._remote_dir}/{file_name}"
        try:
            self._client.delete_file(remote_path)
        except Exception:
            logger.exception("Failed to acknowledge/delete file: %s", file_name)
