FROM quay.io/fedora/fedora-silverblue:44

RUN dnf install -y \
    # Dev tools
    uv \
    stow \
    # code \
    # Utilities
    the_silver_searcher \
    tree \
    which \
    file \
    jq \
    # Security
    pass-otp \
    pass-audit \
    gnupg2 \
    qtpass \
    # VCS
    git \
    gitk \
    git-annex \
    # System
    psmisc \
    lsof \
    && dnf clean all
