# Independent migration oracle

`trinity_memory_reference/` freezes the pre-migration Python implementation.
It is test-only and must not be packaged in production wheels or imported by
`trinity_memory`. Native differential tests use this distinct package name.
The file hashes in `manifest.json` attest the copied baseline bytes.
