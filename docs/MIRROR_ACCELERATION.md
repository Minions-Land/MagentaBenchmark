# Mirror Acceleration

MagentaBench treats a mirror as an untrusted transport or cache. It is never
the canonical source, an experiment factor, a backend identity, or benchmark
evidence. Git pushes go only to the canonical GitHub `origin`; OCI identity is
the canonical repository plus immutable manifest digest; Python dependencies
remain bound by the checked-in lock.

Invoke the module through the locked project environment:

```bash
uv run --frozen python -m tools.mirror_acquisition.cli <subcommand>
```

The subcommands are deliberately narrow:

```text
doctor         read-only Git, Python, Docker, and policy checks
git-configure  idempotent fetch-only Git remote configuration
plan           render canonical and acquisition references
verify         inspect already-cached OCI images, without network
acquire        verify one remote manifest, pull if absent, retag, and
               atomically create one explicit receipt
```

None of these commands runs a container, modifies Docker daemon configuration,
starts a benchmark, makes a result claim, or performs bulk prefetch.

## Standard Policy

| Transport | Canonical identity | Accelerator | Required boundary |
| --- | --- | --- | --- |
| Git | `https://github.com/Minions-Land/MagentaBenchmark.git` | `ghfast.top` fetch remote | `origin` remains the only push target; the mirror uses a deliberately unsupported `disabled://` push URL |
| Python | packages and hashes in `uv.lock` | `https://mirrors.aliyun.com/pypi/simple/` | use `--frozen`; do not rewrite the lock because an index changed |
| OCI | `docker.io/<repository>@sha256:<digest>` | named registry such as `docker.1ms.run` | verify raw manifest, config, compressed layer descriptors, platform, and local rootfs before canonical tagging |

The mirror registry is explicit configuration. Changing it changes only the
acquisition reference; the spec's canonical reference and digest remain
unchanged.

## Preflight

Run the doctor with the Python index selected for this host:

```bash
UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/ \
  uv run --frozen python -m tools.mirror_acquisition.cli doctor
```

A successful report has format `magentabench-mirror-doctor-v1` and sorted,
passing checks. The doctor reads local Git configuration and contacts only the
local Docker daemon for its version. It does not fetch Git, contact an OCI
registry, pull or tag an image, or print an observed mismatched URL. Failure
codes identify drift without echoing possible credentials. Git checks honor
the effective `HOME`, XDG, system, global, and command-scope `GIT_CONFIG*`
selectors. Docker uses a separate empty client configuration, and reported
version values are normalized to numeric release components.

To create or repair the repository's fetch-only remote after `origin` has been
verified as canonical:

```bash
uv run --frozen python -m tools.mirror_acquisition.cli git-configure
git fetch mirror main
```

`git-configure` refuses to write when `origin` is missing, has multiple URLs,
or differs from the canonical GitHub URL. It writes the disabled push URL
first, preserves `origin`, and is a no-op when the expected state already
exists. Before merging or pushing, verify the fetched commit against canonical
GitHub state. Push with `git push origin ...`; never push to `mirror`.

## OCI Specs

Approved specs live under `acquisition/oci/`. Each JSON file is strict,
bounded, non-symlink input and binds:

- the explicit `docker.io` repository and canonical tag;
- one single-platform manifest digest and descriptor;
- the config digest and descriptor;
- every compressed layer descriptor;
- Linux/amd64 platform identity; and
- every local uncompressed rootfs diff ID.

The initial cached Terminal-Bench fixtures are:

| Spec | Canonical manifest |
| --- | --- |
| `terminal-bench-regex-log-20251031.json` | `sha256:90101b2e815323a8da20528a1439bebc407eb9761c9c68a3d557730856c878e9` |
| `terminal-bench-headless-terminal-20251031.json` | `sha256:eb7e209672bf6cef2785fafd9e13509b10626c327bcc2b37f5bf40ca83eaf3aa` |

Inspect a transport plan without Docker or network access:

```bash
uv run --frozen python -m tools.mirror_acquisition.cli plan \
  acquisition/oci/terminal-bench-regex-log-20251031.json
```

Verify an already-cached mirror reference and canonical tag without contacting
the registry:

```bash
uv run --frozen python -m tools.mirror_acquisition.cli verify \
  acquisition/oci/terminal-bench-regex-log-20251031.json
```

Cached verification checks the manifest repo digest, config ID, platform,
rootfs diff IDs, and canonical tag. It explicitly reports that compressed
layer descriptors were not reverified; that stronger boundary belongs to
`acquire`.

## Acquire One Image

`acquire` is the only network-mutating OCI command. It accepts exactly one spec
and an explicit durable receipt path:

```bash
uv run --frozen python -m tools.mirror_acquisition.cli acquire \
  acquisition/oci/terminal-bench-regex-log-20251031.json \
  --mirror-registry docker.1ms.run \
  --receipt /durable/magentabench-receipts/regex-log-20251031.json
```

The operation is ordered fail-closed:

1. acquire the canonical-tag lock, validate the spec, mirror authority, and
   receipt path, then acquire a lock for that absolute receipt path;
2. reuse a complete matching receipt, or prepare a bounded temporary receipt
   in the destination directory before any Docker operation;
3. open and hash the Docker executable, invoke that fixed file descriptor for
   every Docker command, and inspect any existing canonical tag;
4. retrieve the raw manifest by immutable digest and independently hash its
   bytes;
5. compare the manifest, config, compressed layers, and platform with the
   tracked spec;
6. pull only `mirror/repository@sha256:<digest>` when the exact local image is
   absent; Docker verifies layer content against those descriptors;
7. compare the local config ID, platform, repo digest, and rootfs diff IDs;
8. refuse to overwrite a canonical tag that names another image, recheck the
   opened Docker inode before mutation, tag only the verified image, inspect it
   again, and recheck the executable identity; and
9. enforce the 4 MiB receipt limit and atomically create the success receipt
   without overwriting another receipt.

Failure before step 8 never tags the canonical name, and no success receipt is
linked until every verification passes. The destination directory is bound by
device and inode before Docker activity and checked again before mutation and
publication, so a renamed or replaced parent cannot produce a false successful
return. Existing receipts remain open throughout cache verification; their
parent, directory entry, inode metadata, bytes, and digest are checked again
before reuse returns. If directory durability reporting fails after the atomic
link, cleanup invalidates only the already-opened inode and never unlinks by
pathname. The requested path may therefore retain a zero-byte or deliberately
invalid non-success file; inspect and remove it explicitly, or retry with a new
receipt path. Repeating a completed operation verifies the cached image and
reuses the byte-identical matching receipt. A receipt records canonical and
runtime identities, the mirror transport, actions taken, and limitations with
`claim_eligible=false`; hash the receipt file when linking it from a lab
checkpoint.

Canonical-tag and receipt-path locks use digest-named abstract Unix sockets.
They have no replaceable filesystem inode and disappear when the owning process
exits, so cooperating processes in the same Linux network namespace serialize
both mutations and receipt ownership. The lab lease remains the boundary across
machines and isolated network namespaces. Operators must not run direct
`docker tag` commands against the same canonical tag during acquisition;
Docker itself has no conditional no-clobber tag operation. Timed-out or
over-limit Git and Docker commands are terminated as a complete process group,
not only as the immediate client process. A process that ignores these locks
can still replace a receipt path or mutate Docker state; acquisition detects
that drift where possible and never deletes a concurrent receipt replacement,
but the lab lease is required to prevent such non-cooperating writes.

Use only public, unauthenticated registries with this command. It launches
Docker with an empty temporary client configuration and a minimal environment;
private registries and credential forwarding are intentionally out of scope.

## Apptainer And Cloud Runtimes

The current Apptainer profile remains host-readiness-only. These mirror
acquisition commands do not create a SIF, convert a Docker image, register an
Apptainer backend, or raise its exploratory evidence ceiling. Before an
Apptainer acquisition adapter is enabled it must:

- use explicit persistent `APPTAINER_CACHEDIR` and `APPTAINER_TMPDIR` paths;
- start from the same canonical OCI digest while treating any registry prefix
  as transport only;
- write a new immutable SIF destination rather than a mutable shared file;
- retain the OCI spec identity, observed SIF SHA-256, Apptainer executable
  identity, platform, cache boundary, and conversion command; and
- close runtime, network, artifact export, teardown, recovery, and standalone
  verification boundaries independently of this acquisition receipt.

AppContainer, E2B, and other cloud sandboxes have the same identity rule. A
provider cache or template may accelerate staging, but it cannot replace the
canonical input digest or observed runtime/template identity. Their adapters
remain separate work items and require explicit cost, credential, lifecycle,
and artifact-export controls before benchmark execution.

## Recovery And Rotation

If a mirror becomes unavailable, select another credential-free registry and
rerun `plan` first. The canonical digest must remain identical. Do not edit a
spec merely to match a mirror's floating tag, and do not retag an image after a
failed verification.

Rollback requires no daemon changes: stop using the accelerator, remove the
local fetch-only Git remote if desired, and acquire the same canonical digest
through another transport. Existing receipts remain historical acquisition
evidence and must not be rewritten.
