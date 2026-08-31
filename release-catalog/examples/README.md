# Example companion

[`cuvs-java`](cuvs-java) is a complete, representative companion for one cuVS
Java build job. It was derived from the test fixture in
`tests/release_catalog_test.sh`; its source SHA and GitHub Actions run ID are
deliberately non-production example values.

The primary JAR is not part of the companion and is therefore not checked in.
Its example contents are `jar` followed by a newline, whose SHA-256 digest is
`fb8ce05502991565de98e3e21d9ab98151c1cd1715b14a3f7c349cba300cb2b9`.
That digest links the catalog entry, SPDX and CycloneDX documents, provenance
statement, and the separately uploaded primary artifact.

The four JSON files are about 4 KiB in total. Companion size grows
linearly with the number of primary artifacts. This version does not copy
producer dependency SBOMs into the companion; those could be substantially
larger and require a future explicit catalog association.
