#!/usr/bin/env bash
# Fetch a normal checkout and run the existing Python setup. No global Kern CLI.
set -euo pipefail

confirm() {
  local answer
  printf '%s [y/N] ' "$1" >/dev/tty
  read -r answer </dev/tty || return 1
  case "$answer" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

check_dependencies() {
  missing=()
  for dependency in git curl ssh ssh-keygen scp; do
    if ! command -v "$dependency" >/dev/null || { [[ "$dependency" == git ]] && ! git --version >/dev/null 2>&1; }; then
      missing+=("$dependency")
    fi
  done
  if ! command -v python3 >/dev/null || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    missing+=(python3)
  fi
  if ! command -v limactl >/dev/null; then
    missing+=(limactl)
  else
    local version
    version=$(limactl --version) || return 1
    if [[ ! "$version" =~ ([0-9]+)\.([0-9]+)\.([0-9]+) ]]; then
      echo "Cannot read Lima version: $version" >&2; return 1
    fi
    if (( BASH_REMATCH[1] != 2 || BASH_REMATCH[2] < 2 )); then
      echo "Kern needs Lima >=2.2 and <3; found $version. Select a supported version at https://lima-vm.io/docs/installation/ and rerun." >&2
      return 1
    fi
  fi
  if [[ "$system" == Linux ]] && ! command -v "qemu-system-$(uname -m)" >/dev/null; then
    missing+=(qemu)
  fi
  if [[ "$system" == Linux ]] && ! command -v qemu-img >/dev/null; then missing+=(qemu-img); fi
}

install_dependencies() {
  local dependency package
  local packages=()
  if command -v brew >/dev/null; then
    for dependency in "${missing[@]}"; do
      case "$dependency" in
        python3) package=python ;;
        limactl) package=lima ;;
        ssh|ssh-keygen|scp) package=openssh ;;
        qemu-img) package=qemu ;;
        *) package=$dependency ;;
      esac
      [[ " ${packages[*]} " == *" $package "* ]] || packages+=("$package")
    done
    printf 'Install with Homebrew: brew install'; printf ' %q' "${packages[@]}"; printf '\n'
    confirm 'Install these dependencies?' || { echo 'Nothing installed. Install the listed dependencies yourself, then rerun.'; return 1; }
    brew install "${packages[@]}" </dev/tty
  elif [[ "$system" == Linux ]] && command -v apt-get >/dev/null && command -v sudo >/dev/null; then
    local install_lima=false archive='' checksum=''
    for dependency in "${missing[@]}"; do
      case "$dependency" in
        limactl) install_lima=true; continue ;;
        python3)
          local candidate
          candidate=$(LC_ALL=C apt-cache policy python3 2>/dev/null) || candidate=''
          if [[ ! "$candidate" =~ Candidate:[[:space:]]+([0-9]+:)?([0-9]+)\.([0-9]+) ]] ||
             (( BASH_REMATCH[2] < 3 || (BASH_REMATCH[2] == 3 && BASH_REMATCH[3] < 11) )); then
            echo 'apt does not advertise Python 3.11 or newer. Install a supported Python from https://www.python.org/downloads/ or your supported distribution packages, ensure python3 selects it, then rerun. No dependencies were installed.' >&2
            return 1
          fi
          package=python3 ;;
        ssh|ssh-keygen|scp) package=openssh-client ;;
        qemu-img) package=qemu-utils ;;
        qemu)
          case "$(uname -m)" in x86_64) package=qemu-system-x86 ;; aarch64) package=qemu-system-arm ;; *) echo 'Unsupported QEMU architecture. Install prerequisites manually.' >&2; return 1 ;; esac ;;
        *) package=$dependency ;;
      esac
      [[ " ${packages[*]} " == *" $package "* ]] || packages+=("$package")
    done
    if $install_lima; then
      # Pinned official Lima 2.2.1 archives; do not trust a moving latest asset.
      case "$(uname -m)" in
        x86_64) checksum=b391ac7fcac2b2a5a1628107756463188ded4d18d3104a40a66a8bb6fec01bcb ;;
        aarch64) checksum=fbcf4d127936666aa2cf6013b663c250431f2a59394757b404070f8d6a160e10 ;;
        *) echo 'Install Lima manually for this architecture.' >&2; return 1 ;;
      esac
      archive="lima-2.2.1-Linux-$(uname -m).tar.gz"
      echo "Download $archive from github.com/lima-vm/lima, verify its pinned SHA-256, then extract into /usr/local with sudo tar --no-same-owner."
    fi
    if ((${#packages[@]})); then
      printf 'System packages: sudo apt-get update && sudo apt-get install'; printf ' %q' "${packages[@]}"; printf '\n'
    fi
    echo 'Administrator permission may be requested by sudo.'
    confirm 'Install these dependencies?' || { echo 'Nothing installed. Install the listed dependencies yourself, then rerun.'; return 1; }
    if ((${#packages[@]})); then
      sudo apt-get update </dev/tty
      sudo apt-get install -y "${packages[@]}" </dev/tty
    fi
    if $install_lima; then
      (
        temporary=$(mktemp -d)
        trap 'rm -rf "$temporary"' EXIT
        curl --fail --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 300 \
          "https://github.com/lima-vm/lima/releases/download/v2.2.1/$archive" -o "$temporary/$archive"
        if ! printf '%s  %s\n' "$checksum" "$temporary/$archive" | sha256sum --check --status; then
          echo 'Lima checksum verification failed; no archive was extracted. Rerun to download again.' >&2
          exit 1
        fi
        sudo tar --extract --gzip --no-same-owner --file "$temporary/$archive" --directory /usr/local </dev/tty
      )
    fi
  else
    echo 'Install prerequisites using https://lima-vm.io/docs/installation/ and your system package manager, then rerun.' >&2
    if [[ "$system" == Darwin ]]; then echo 'On macOS, install Homebrew from https://brew.sh first.' >&2; fi
    return 1
  fi
  hash -r
}

main() {
  system=$(uname -s)
  case "$system" in Darwin|Linux) ;; *) echo 'Kern quick-start requires macOS or Linux.' >&2; return 1 ;; esac
  if (( EUID == 0 )); then echo 'Run quick-start as your normal user, without sudo.' >&2; return 1; fi
  if ! ( : </dev/tty ) 2>/dev/null; then echo 'Run quick-start from an interactive terminal.' >&2; return 1; fi
  echo 'Kern quick-start — local VM and browser workspace'
  echo '1/3 Checking Python 3.11+, Lima >=2.2,<3, Git, curl, SSH and the VM backend…'
  check_dependencies
  if ((${#missing[@]})); then
    printf 'Missing or outdated:'; printf ' %s' "${missing[@]}"; printf '\n'
    install_dependencies
    check_dependencies
    if ((${#missing[@]})); then
      printf 'Still unavailable:' >&2; printf ' %s' "${missing[@]}" >&2; printf '\nInstall supported versions, ensure they are on PATH, and rerun.\n' >&2
      return 1
    fi
  fi
  echo 'Dependencies ready.'
  local source_dir="${KERN_SOURCE_DIR:-$HOME/kern}"
  local repository='https://github.com/infiloop2/kern.git'
  if [[ -L "$source_dir" ]]; then echo "Refusing a symlink checkout: $source_dir" >&2; return 1; fi
  echo "2/3 Preparing source in ${source_dir}..."
  if [[ ! -e "$source_dir" ]] || [[ -d "$source_dir" && -z "$(ls -A "$source_dir")" ]]; then
    git clone --depth 1 --branch main "$repository" "$source_dir"
  else
    local remote=''
    if [[ "$(git -C "$source_dir" rev-parse --is-inside-work-tree 2>/dev/null)" == true ]]; then
      remote=$(git -C "$source_dir" remote get-url origin) || return 1
    fi
    case "$remote" in
      https://github.com/infiloop2/kern|https://github.com/infiloop2/kern.git|git@github.com:infiloop2/kern|git@github.com:infiloop2/kern.git|ssh://git@github.com/infiloop2/kern|ssh://git@github.com/infiloop2/kern.git) ;;
      *) echo "$source_dir already exists and is not a Kern checkout. Choose an empty location with KERN_SOURCE_DIR." >&2; return 1 ;;
    esac
    echo "Using $source_dir without changing its source. Use git pull there when you want to update."
  fi
  if [[ ! -f "$source_dir/quickstart.py" ]]; then
    echo "$source_dir does not contain quickstart.py. Update that checkout yourself, preserving local edits, or choose an empty location with KERN_SOURCE_DIR and rerun." >&2
    return 1
  fi
  cd "$source_dir"
  echo "Kern source: $(git rev-parse HEAD)"
  echo '3/3 Starting Kern. First setup downloads a VM and can take several minutes.'
  python3 quickstart.py </dev/tty
}

main "$@"
