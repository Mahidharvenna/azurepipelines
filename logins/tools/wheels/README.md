# Vendored wheels

`prepare_python.sh` installs `pymssql` from here first, with no network at all,
because the Linux build agent has no route to PyPI. Only if no wheel here fits
the agent's Python does it fall back to a package index (`PIP_INDEX_URL` /
`HTTPS_PROXY`, see `DEPLOY.md`, phase 0).

| File | For | SHA-256 (matches PyPI) |
|---|---|---|
| `pymssql-2.3.13-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl` | Python 3.12, Linux x86_64, glibc ≥ 2.28 | `c0ea72641cb0f8bce7ad8565dbdbda4a7437aa58bce045f2a3a788d71af2e4be` |
| `pymssql-2.3.13-cp39-cp39-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl` | Python 3.9, Linux x86_64, glibc ≥ 2.28 | `c690f1869dadbf4201b7f51317fceff6e5d8f5175cec6a4a813e06b0dca2d6ed` |

Unmodified copies of the files published at <https://pypi.org/project/pymssql/2.3.13/>.
Each wheel carries its own SQL Server client (FreeTDS compiled in, plus
OpenSSL and Kerberos), so nothing else needs installing — and it has no
Python dependencies.

**License:** pymssql is LGPL-2.1 (the full text is inside each wheel, under
`pymssql-2.3.13.dist-info/licenses/LICENSE`). Source:
<https://github.com/pymssql/pymssql/tree/v2.3.13>.

## Updating

Download the new version's `manylinux` x86_64 wheels for the agent's Python
versions from PyPI, check each file's SHA-256 against the one PyPI lists, and
change `PYMSSQL=` in `prepare_python.sh` to match.
