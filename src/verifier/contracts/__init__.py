"""Frozen contracts. Pure pydantic, zero internal imports beyond this package.

Every module in the system compiles against these types. Changing anything here is a
contract change: announce it, and update the snapshot in
tests/contracts/test_schema_snapshot.py (which will fail the build otherwise).
"""

from verifier.contracts import api, citations, documents, enums, findings, layers, runs

__all__ = ["api", "citations", "documents", "enums", "findings", "layers", "runs"]
