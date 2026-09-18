# Getting a containment backend this deployment may use

```text
Status:   RESOLVED on this host, 2026-09-18
          Option A was taken: Debian's 0.12.0-1 rebuilt for noble
Backend:  bubblewrap 0.12.0 (pkg 0.12.0-1~deb13u1), /usr/bin/bwrap,
          NOT setuid, sha256 1e2250f2605d7584...
State:    security eligible  OK
          namespace          OK   (targeted AppArmor profile installed)
          containment        OK   (27/27 adversarial checks, one real task)
```

`kernel.apparmor_restrict_unprivileged_userns` is still **1**. It was not
weakened; the targeted `deploy/apparmor-bwrap` profile grants `userns` to that
one binary, which is the model this document recommended.

Two defects surfaced the moment containment started working, both in Research
OS rather than on the host, and both are fixed:

- the namespace probe bound only `/usr`, so on a usrmerged host no dynamically
  linked sentinel could start, and it reported the resulting ENOENT as a kernel
  denial;
- `RLIMIT_NPROC` was applied before `exec(bwrap)`, so it was checked against the
  researcher's session (1163 tasks) rather than the sandbox's, and every
  contained command failed with `Creating new namespace failed`.

The history below is kept because the version trap in it is still live for
anyone on the stock Ubuntu package.

---

## The history: what was wrong, in order of precedence

**1. The installed bubblewrap has a known sandbox escape, again.**

```text
package        bubblewrap 0.9.0-1ubuntu0.3   (noble-security)
binary         /usr/bin/bwrap, mode 0755, NOT setuid, 72160 bytes
advisory       CVE-2026-87766 / GHSA-pxhw-h44j-8pfx
affects        every upstream version below 0.12.0
fixed upstream 0.12.0, released 2026-08-26
```

**Ubuntu fixed this and then withdrew the fix.** The archive history is:

```text
0.9.0-1ubuntu0.1   unfixed
0.9.0-1ubuntu0.2   FIXED     USN-8779-1, two CVE-2026-87766 patches
                             (safe_openat from crun; reject symlink mount
                             destinations)
0.9.0-1ubuntu0.3   UNFIXED   "SECURITY REGRESSION: Incompatibility with
                             Flatpak (LP: #2167621) - debian: Drop
                             CVE-2026-87766"
```

The patches broke Flatpak applications requesting CUPS access with "Too many
levels of symbolic links" (`containers/bubblewrap#801`, `flatpak/flatpak#6830`)
and Canonical reverted them rather than hold the regression.
`0.9.0-1ubuntu0.2` is **no longer in the archive** -- `apt-cache policy` offers
only `0.9.0-1ubuntu0.3` from noble-security and `0.9.0-1ubuntu0.1` from
noble-updates. Corroborating the changelog: the installed binary is 72160
bytes, byte-size identical to the unpatched `0.9.0-1ubuntu0.1`, and carries no
`safe_openat`.

So on this host today **there is no installable Ubuntu bubblewrap carrying the
fix**, and upgrading does not help. Downgrading to `0.9.0-1ubuntu0.2` would
reintroduce the Flatpak regression and the version is not fetchable anyway.

During sandbox *setup*, creating a file or directory under the new root can
follow a parent symlink out through `/oldroot` and write an attacker-chosen
path on the host, as the launching user. It happens before anything is running
inside the sandbox, so it is not a runtime escape — it is a setup-time write
primitive.

That threat model is this system's ordinary case, not an exotic one. The coding
pipeline runs a project's acceptance commands over a worktree a model has just
written to, which is exactly "bubblewrap is used to create files on
attacker-controlled filesystem content".

**2. This kernel refuses unprivileged user namespaces.**

```text
kernel.apparmor_restrict_unprivileged_userns = 1     (Ubuntu 24.04 default)
kernel.unprivileged_userns_clone             = 1
no AppArmor profile exists for /usr/bin/bwrap
```

So bwrap cannot currently create a namespace at all.

**The two together are why the order matters.** Right now the vulnerable binary
cannot be used, because the kernel will not give it a namespace. Installing
`deploy/apparmor-bwrap` first would grant the namespace and leave the escape
intact — converting a sandbox that contains nothing into a sandbox that
contains things and can be walked out of. That is strictly worse than the
present state.

> Replace the binary first. Then grant the namespace.

`researchctl runtime doctor` says this, `deploy/apparmor-bwrap`'s header says
this, and `available_backend()` enforces it: a bubblewrap below the security
floor is never returned as a production backend however well it runs.

## What was ruled out, and why

| Option | Verdict |
|---|---|
| Ubuntu `noble` security update | **Withdrawn.** `0.9.0-1ubuntu0.2` carried the fix and was superseded by `0.9.0-1ubuntu0.3`, which drops it (see above). Nothing currently installable from noble carries the patches. `noble-backports` carries nothing. |
| Waiting for Ubuntu to re-fix it | **Plausible, and not yet real.** Upstream is reworking the fix so it does not break Flatpak. When a `0.9.0-1ubuntu0.4` or later appears, read its changelog and, if it restores the patches, add it to `VENDOR_FIXED_RANGES` in `src/research_os/sandbox.py`. It will not be trusted for being newer. |
| Another Research OS backend | **None exists.** `podman` and `docker` are probed and reported, and neither has a backend implemented — `ARCHITECTURE.md` §12 keeps both on the postponed list. `podman` is not installed here either. |
| `systemd-run --user` | **Never usable.** Its sandboxing directives need the same unprivileged user namespaces the kernel is refusing, and it starts the unit anyway without binding them. Probed, recorded, and never selected. |
| Turning off the restriction machine-wide | **Rejected.** `sysctl kernel.apparmor_restrict_unprivileged_userns=0` lifts it for every binary on the machine, and does nothing about reason 1. |
| A setuid bwrap | **Rejected.** 0.12.0 removed setuid support; reintroducing a setuid sandbox binary to work around a namespace restriction trades a bounded problem for an unbounded one. |
| An arbitrary PPA or a downloaded binary | **Rejected.** No provenance. |

## The three options that remain

Each needs root. None has been executed. Pick one.

---

### Option A — rebuild Debian's fixed package on this machine *(recommended)*

Debian has already done the packaging work and shipped it as a stable-release
security update:

```text
Debian trixie (stable)   bubblewrap 0.12.0-1~deb13u1
Debian testing/unstable  bubblewrap 0.12.0-1
```

Rebuilding that source package on noble produces a binary linked against
*this* machine's libraries, from vendor packaging, with a signed source
lineage. Dependency constraints are already satisfied here:

```text
required            present on this host
libc6    >= 2.38    2.39-0ubuntu8.9     OK
libcap2  >= 1:2.10  1:2.66-5ubuntu2.4   OK
libselinux1 >= 3.1~ 3.5-2ubuntu2.1      OK
```

```bash
# 1. Build tooling and the package's own build-dependencies.
sudo apt-get install --no-install-recommends \
    build-essential devscripts dpkg-dev meson ninja-build \
    libcap-dev libselinux1-dev docbook-xsl xsltproc

# 2. Fetch Debian's source package, with signature verification.
#    dget checks the .dsc's OpenPGP signature and every file's checksum.
cd "$(mktemp -d)"
dget -x https://deb.debian.org/debian/pool/main/b/bubblewrap/bubblewrap_0.12.0-1.dsc

# 3. Build it against noble's libraries.
cd bubblewrap-0.12.0
dpkg-buildpackage -us -uc -b

# 4. Inspect before installing. The version must be 0.12.0-1 and the
#    binary must NOT be setuid.
cd ..
dpkg-deb -I bubblewrap_0.12.0-1_amd64.deb
dpkg-deb -c bubblewrap_0.12.0-1_amd64.deb | grep bwrap

# 5. Install.
sudo apt-get install ./bubblewrap_0.12.0-1_amd64.deb
```

**What it changes:** replaces `/usr/bin/bwrap` with a 0.12.0 build.
**Expected output:** `bwrap --version` prints `bubblewrap 0.12.0`.
**Rollback:** `sudo apt-get install --reinstall --allow-downgrades bubblewrap=0.9.0-1ubuntu0.3`
**Note:** a locally built package will be overwritten if Ubuntu later publishes
its own fix with a higher version. That is the desired behaviour. Hold it with
`sudo apt-mark hold bubblewrap` only if you want to prevent a *downgrade*, and
remember to unhold when Ubuntu ships a real fix.

---

### Option B — build upstream 0.12.0 from source

Smaller dependency surface than a Debian backport, and no Debian packaging in
the path; correspondingly, no package manager knows about the result.

```text
tarball  bubblewrap-0.12.0.tar.xz
sha256   9760d007363e3abba7c747489910f9f82d9fca53ba3bd3282e396fa3c97a3314
release  v0.12.0, 2026-08-26, signed by Alexander Larsson (key 616C5BDC0C29AB04)
```

```bash
sudo apt-get install --no-install-recommends \
    build-essential meson ninja-build libcap-dev libselinux1-dev

cd "$(mktemp -d)"
curl -fLO https://github.com/containers/bubblewrap/releases/download/v0.12.0/bubblewrap-0.12.0.tar.xz
curl -fLO https://github.com/containers/bubblewrap/releases/download/v0.12.0/bubblewrap-0.12.0.tar.xz.sha256sum

# Verify BEFORE unpacking. Do not proceed if this does not print OK.
sha256sum -c bubblewrap-0.12.0.tar.xz.sha256sum
# Cross-check against the digest recorded above, independently of the
# checksum file that was downloaded beside the tarball.
sha256sum bubblewrap-0.12.0.tar.xz

tar xf bubblewrap-0.12.0.tar.xz && cd bubblewrap-0.12.0
meson setup build --prefix=/usr/local -Dsetuid=disabled -Dman=disabled
ninja -C build
sudo ninja -C build install

# The installed binary must not be setuid, and must be the one on PATH.
ls -l /usr/local/bin/bwrap
command -v bwrap && bwrap --version
```

**What it changes:** installs a 0.12.0 bwrap at `/usr/local/bin/bwrap`, ahead
of `/usr/bin/bwrap` on the default PATH. The 0.9.0 package stays installed.
**Rollback:** `sudo rm /usr/local/bin/bwrap`
**Extra step:** `deploy/apparmor-bwrap` names `/usr/bin/bwrap`. With this
option the profile must be edited to name `/usr/local/bin/bwrap` instead, or it
will grant the permission to the wrong binary and nothing will work.

---

### Option C — wait

```text
WAITING_FOR_EXTERNAL_DEPENDENCY
```

Do nothing until Ubuntu publishes a fixed `bubblewrap` for noble. Containment
stays unavailable, and Research OS keeps refusing to execute model-written
experiments — it will design and preregister them and stop. That refusal is
correct behaviour and blocks only the execution stage of the pilot.

Check with:

```bash
apt-get update && apt-cache policy bubblewrap
```

---

## After the binary is replaced

Only now is the AppArmor profile the right move.

```bash
# 1. Confirm the binary Research OS would actually use is at the floor.
bwrap --version
uv run --frozen --extra runtime researchctl runtime doctor | grep -i sandbox

# 2. Grant the namespace to that one binary. Edit the `profile` line first
#    if the patched binary is not at /usr/bin/bwrap.
sudo install -m 644 deploy/apparmor-bwrap /etc/apparmor.d/bwrap
sudo apparmor_parser -r /etc/apparmor.d/bwrap

# 3. Verify through the real sandbox adapter, NOT through
#    `unshare --user --map-root-user true` -- that tests a different binary
#    under a different profile and can pass while bwrap still fails.
uv run --frozen --extra runtime researchctl runtime doctor | grep -i sandbox
uv run --frozen --extra runtime pytest tests/test_sandbox.py -q
```

Expected after step 3: the doctor reports the bubblewrap backend as `OK`, and
the ten adversarial tests in `tests/test_sandbox.py` stop skipping and pass.

**A namespace is not a boundary.** Until those adversarial tests have actually
run and held against this exact binary, `containment_validated` stays false and
`researchctl runtime doctor` reports `WARN containment NOT PROVEN`. The
validation record is keyed to the binary's content hash, so replacing bwrap
correctly invalidates it.

## Rollback for the AppArmor profile

```bash
sudo rm /etc/apparmor.d/bwrap
sudo systemctl reload apparmor
```

## The security tradeoff, stated plainly

Granting `userns` to one binary is a real increase in what that binary can do.
The argument for it is that the alternative is running model-written acceptance
commands with the researcher's full environment — their SSH agent, their Git
credentials, their provider keys — and no OS boundary at all, which is what
happens today under `SandboxMode.PREFERRED`.

What a working bubblewrap buys is mount, PID, network and user namespace
separation. What it does not buy is safety from code you have reason to
distrust. `SECURITY.md` states that boundary in full.
