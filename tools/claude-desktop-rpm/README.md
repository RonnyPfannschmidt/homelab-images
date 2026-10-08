# Claude Desktop as a local RPM

Anthropic publishes Claude Desktop for Linux (in beta) only as a Debian package,
from a signed apt repository. This tool turns that package into an RPM for
Fedora, **for installing on your own machine**. It is not an Anthropic build and
Anthropic does not support it.

The app is proprietary: its `copyright` file says `License: Proprietary`, and
nothing grants a right to redistribute it. So the RPM is never published, and
this is not a Copr. Copr's terms only allow software under licences Fedora
accepts, and a Copr build would also publish the binary inside the SRPM. The
downloads and the RPMs land in `dist/`, which git ignores.

## Use

```bash
tools/claude-desktop-rpm/build.py --check    # newest upstream version, verified
tools/claude-desktop-rpm/build.py            # build it for this machine
sudo dnf install tools/claude-desktop-rpm/dist/claude-desktop-*.x86_64.rpm
```

`--version` builds a specific upstream version, and `--arch aarch64` builds for
the Pi from an x86 machine. Building needs `podman`, `gpg` and `gpgv` on the
host; `rpmbuild` runs in a throwaway `fedora:44` container (`--image` changes
that).

There is no repository, so updates do not arrive through `dnf upgrade`. Run
`--check`, compare with `rpm -q claude-desktop`, and rebuild when it moves.
`dnf install` of the newer RPM upgrades in place.

## What is checked

The same chain apt follows. `InRelease` must be validly signed by Anthropic's
release key, pinned in `claude-desktop-archive-keyring.asc` and in `build.py` by
fingerprint (`31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE`, as published
in the [Linux install docs](https://code.claude.com/docs/en/desktop-linux)).
`Packages` must match the hash the signed `InRelease` gives for it, and the
`.deb` must match the size and hash `Packages` gives for it. Anything else stops
the build.

## How the RPM differs from the deb

The deb's maintainer scripts do five things on install. The RPM:

- **ships** the GNOME Shell search provider and the `.mcpb`/`.skill` MIME types
  as ordinary package files, rather than copying them out of the app tree on
  every install. Fedora's file triggers refresh the MIME and desktop databases.
- **links** `/usr/bin/ccd` when the upstream build contains it, as the deb does.
- **drops** the apt repository and key registration, and the AppArmor profile.
  The profile exists to get around Ubuntu's user-namespace restriction, which
  Fedora does not have.

`chrome-sandbox` stays setuid root, as upstream ships it. The deb's `Depends`
become `Requires` and its `Recommends` become weak dependencies under Fedora
names. Most of the shared libraries Electron needs are loaded with `dlopen`,
which rpm's dependency generator does not see, so they are listed by hand.

## Cowork

Cowork runs its tasks in a QEMU/KVM guest. The RPM recommends QEMU, UEFI
firmware and `virtiofsd`, but Cowork also needs the user in the `kvm` group
(for `/dev/vhost-vsock`) and the `vhost_vsock` module loaded. See
[Cowork requirements](https://code.claude.com/docs/en/desktop-linux#cowork-requirements).

## When this can go

Anthropic's docs say support for Fedora and RHEL is coming, and the deb's own
maintainer scripts mention RPM scriptlets already. When an official RPM or dnf
repository appears, this directory should be deleted.
