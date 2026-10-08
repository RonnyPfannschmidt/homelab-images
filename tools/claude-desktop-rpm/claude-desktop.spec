# Repackages Anthropic's official Debian build; build.py beside this file
# fetches and verifies the .deb and defines upstream_version. See README.md.
%{!?upstream_version:%{error:define upstream_version; build.py does}}

%ifarch x86_64
%global debarch amd64
%endif
%ifarch aarch64
%global debarch arm64
%endif

%global appdir %{_prefix}/lib/claude-desktop

# Prebuilt Electron: nothing to debug, strip or link by build-id. The build-id
# links would also collide with every other Electron app on the system.
%global debug_package %{nil}
%global __strip /bin/true
%global _build_id_links none
# Reproducible: build.py sets SOURCE_DATE_EPOCH to the .deb's build time, and
# the RPM takes its build time, file mtimes and host from that, not the build.
# There is no %changelog to date it from; upstream's history is theirs.
%global source_date_epoch_from_changelog 0
%global use_source_date_epoch_as_buildtime 1
%global clamp_mtime_to_source_date_epoch 1
%global _buildhost claude-desktop-rpm
# The bundled libraries are private to the app, so they must neither satisfy
# nor create dependencies on the rest of the system.
%global __provides_exclude_from ^%{appdir}/.*$
%global __requires_exclude ^(libffmpeg|libvulkan|libvk_swiftshader)\\.so.*$

Name:           claude-desktop
Version:        %{upstream_version}
# Bump when this spec changes for the same upstream version; reset on a new one.
Release:        1%{?dist}
Summary:        Desktop application for Claude.ai (unofficial repackaging)
License:        LicenseRef-Proprietary AND MIT
URL:            https://claude.ai/download
Source0:        claude-desktop_%{version}_%{debarch}.deb
ExclusiveArch:  x86_64 aarch64

BuildRequires:  binutils
BuildRequires:  tar
BuildRequires:  xz

# The deb's Depends. Electron dlopens most of these, so the ELF dependency
# generator does not see them.
Requires:       gtk3
Requires:       libnotify
Requires:       nss
Requires:       at-spi2-core
Requires:       libdrm
Requires:       mesa-libgbm
Requires:       libxcb
Requires:       libsecret
Requires:       libXtst
Requires:       libuuid
Requires:       pipewire-libs
Requires:       glib2
Requires:       xdg-utils
Requires:       xdg-desktop-portal
Requires:       (xdg-desktop-portal-gtk or xdg-desktop-portal-gnome or xdg-desktop-portal-kde)
Requires:       hicolor-icon-theme

# The deb's Recommends, under Fedora's names.
Recommends:     alsa-lib
Recommends:     libayatana-appindicator-gtk3
Recommends:     (gnome-keyring or plasma-workspace)
Recommends:     bubblewrap
Recommends:     socat
# The GNOME Shell search provider is a gjs script.
Recommends:     gjs
# Cowork runs its tasks in a QEMU/KVM guest.
Recommends:     virtiofsd
%ifarch x86_64
Recommends:     qemu-system-x86-core
Recommends:     edk2-ovmf
%endif
%ifarch aarch64
Recommends:     qemu-system-aarch64-core
Recommends:     edk2-aarch64
%endif

%description
Claude Desktop, repackaged from Anthropic's official Debian package for
Fedora. Not built or supported by Anthropic, and not for redistribution.

%prep
%setup -q -c -T
ar x %{SOURCE0}
tar -xf data.tar.*
mv usr/share/doc/claude-desktop/copyright .

%build

%install
# cp -a keeps chrome-sandbox setuid root, as the deb ships it. That only holds
# when rpmbuild runs as root, which it does in build.py's container.
cp -a usr %{buildroot}/
rm -rf %{buildroot}%{_datadir}/lintian %{buildroot}%{_docdir}

# The deb's postinst copies these out of the app tree on every configure, and
# its postrm deletes them. Installed here instead, rpm owns them, and Fedora's
# file triggers refresh the MIME database.
install -Dpm0644 %{buildroot}%{appdir}/resources/gnome-search-provider/com.anthropic.Claude.search-provider.ini \
    %{buildroot}%{_datadir}/gnome-shell/search-providers/com.anthropic.Claude.search-provider.ini
install -Dpm0644 %{buildroot}%{appdir}/resources/gnome-search-provider/com.anthropic.Claude.SearchProvider.service \
    %{buildroot}%{_datadir}/dbus-1/services/com.anthropic.Claude.SearchProvider.service
install -Dpm0644 %{buildroot}%{appdir}/resources/linux-mime/com.anthropic.Claude.xml \
    %{buildroot}%{_datadir}/mime/packages/com.anthropic.Claude.xml

# The `ccd` terminal launcher ships in some builds and not others; postinst
# links it only when present, so the file list has to follow. rpmbuild rejects
# an empty -f list, so the list always carries the app tree too.
echo %{appdir}/ > extra-files
if [ -x %{buildroot}%{appdir}/resources/bin/ccd ]; then
    ln -s ../lib/claude-desktop/resources/bin/ccd %{buildroot}%{_bindir}/ccd
    echo %{_bindir}/ccd >> extra-files
fi

%files -f extra-files
%license copyright
%{_bindir}/claude-desktop
%{_datadir}/applications/com.anthropic.Claude.desktop
%{_datadir}/icons/hicolor/*/apps/claude-desktop.png
%{_datadir}/gnome-shell/search-providers/com.anthropic.Claude.search-provider.ini
%{_datadir}/dbus-1/services/com.anthropic.Claude.SearchProvider.service
%{_datadir}/mime/packages/com.anthropic.Claude.xml
