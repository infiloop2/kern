APT_COMMAND_TIMEOUT=300s
APT_ACQUIRE_RETRIES=0
APT_ACQUIRE_TIMEOUT=10
APT_UPDATE_COMMAND_TIMEOUT=60s
APT_FAILED_MIRRORS=""

apt_get_once() {
  local command_timeout="$1" acquire_retries="$2" acquire_timeout="$3"
  shift 3
  local -a options=(
    -q
    -o DPkg::Lock::Timeout=300
    -o APT::Keep-Downloaded-Packages=true
    -o Acquire::Retries="$acquire_retries"
    -o Acquire::http::Timeout="$acquire_timeout"
    -o Acquire::https::Timeout="$acquire_timeout"
    -o Acquire::Languages=none
    -o APT::Update::Error-Mode=any
  )
  if [[ "${1:-}" == "install" ]]; then
    # Bound network waits without killing dpkg halfway through unpack/configure.
    # The second command consumes only authenticated, downloaded archives.
    timeout --signal=TERM --kill-after=30s "$command_timeout" \
      /usr/bin/apt-get "${options[@]}" "$@" --download-only || return 1
    /usr/bin/apt-get "${options[@]}" "$@" --no-download
  else
    timeout --signal=TERM --kill-after=30s "$command_timeout" \
      /usr/bin/apt-get "${options[@]}" "$@"
  fi
}

# Keep the original official candidates across the child processes used by
# Git delivery and Playwright. Probe results are scoped to this bootstrap only.
prepare_ubuntu_mirrors() {
  local file line uri candidate
  local official='https?://([a-z0-9-]+\.ec2\.archive\.ubuntu\.com|archive\.ubuntu\.com|security\.ubuntu\.com)/ubuntu'
  APT_SOURCE_FILES=()
  APT_CURRENT_MIRRORS=""
  shopt -s nullglob
  for file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
    [[ -f "$file" ]] && APT_SOURCE_FILES+=("$file")
  done
  shopt -u nullglob
  for file in "${APT_SOURCE_FILES[@]}"; do
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ "$line" =~ ^[[:space:]]*(deb[[:space:]]|deb-src[[:space:]]|URIs:) ]] || continue
      while [[ "$line" =~ $official ]]; do
        uri="${BASH_REMATCH[0]}"
        APT_CURRENT_MIRRORS+=" $uri"
        line="${line#*"$uri"}"
      done
    done < "$file"
  done
  # Leave ports.ubuntu.com and non-Ubuntu repositories alone.
  [[ -n "$APT_CURRENT_MIRRORS" ]] || return 0
  local candidates=""
  for candidate in ${KERN_APT_MIRRORS:-} $APT_CURRENT_MIRRORS http://archive.ubuntu.com/ubuntu http://security.ubuntu.com/ubuntu; do
    [[ "$candidate" =~ ^${official}$ ]] || continue
    # HTTPS can work well even when the same mirror's HTTP endpoint stalls.
    for uri in "http://${candidate#*://}" "https://${candidate#*://}"; do
      [[ " $candidates " == *" $uri "* ]] || candidates+=" $uri"
    done
  done
  export KERN_APT_MIRRORS="$candidates"
}

select_ubuntu_mirror() {
  local mode="$1" candidate tmp result speed elapsed selected file i=0
  [[ -n "$APT_CURRENT_MIRRORS" ]] || return 1
  command -v curl >/dev/null 2>&1 || return 1
  local codename
  codename="$(. /etc/os-release; printf '%s' "${VERSION_CODENAME:-}")"
  [[ "$codename" =~ ^[a-z]+$ ]] || return 1
  if [[ "$mode" == failover ]]; then
    APT_FAILED_MIRRORS+=" $APT_CURRENT_MIRRORS"
  fi
  tmp="$(mktemp -d)" || return 1
  local -a probes=()
  for candidate in ${KERN_APT_MIRRORS:-}; do
    [[ " $APT_FAILED_MIRRORS " == *" $candidate "* ]] && continue
    i=$((i + 1))
    # These parallel probes only measure transport; APT still verifies the
    # signed indexes and package hashes. One slow host costs at most 6 seconds.
    (
      if result="$(curl -fsS --connect-timeout 2 --max-time 6 --max-filesize 1048576 \
        -o /dev/null -w '%{speed_download} %{time_total}' \
        "$candidate/dists/${codename}-security/InRelease" 2>/dev/null)"; then
        printf '%s %s\n' "$result" "$candidate" > "$tmp/$i"
      fi
    ) &
    probes+=("$!")
  done
  for i in "${probes[@]}"; do wait "$i" || true; done
  selected=""
  if compgen -G "$tmp/*" >/dev/null; then
    read -r speed elapsed selected < <(sort -k1,1nr "$tmp"/*)
  fi
  rm -rf -- "$tmp"
  if [[ -z "$selected" ]]; then
    echo "APT: no responsive alternative mirror found within the probe deadline" >&2
    return 1
  fi
  for file in "${APT_SOURCE_FILES[@]}"; do
    # Rewrite active official Ubuntu URIs only; keep suites, keys, options,
    # commented lines and unrelated repositories intact.
    sed -i -E "/^[[:space:]]*(deb[[:space:]]|deb-src[[:space:]]|URIs:)/s#https?://([a-z0-9-]+\.ec2\.archive\.ubuntu\.com|archive\.ubuntu\.com|security\.ubuntu\.com)/ubuntu#${selected}#g" "$file" || return 1
  done
  APT_CURRENT_MIRRORS="$selected"
  echo "APT: selected $selected (security index probe ${elapsed}s, ${speed} bytes/s)" >&2
  return 0
}

apt_get() {
  # Third-party installers enter here too. Avoid routine upgrades while still
  # allowing dependency resolution for required new packages.
  if [[ "${1:-}" == install ]]; then
    shift
    set -- install --no-upgrade "$@"
  fi
  prepare_ubuntu_mirrors || return 1
  if [[ "${1:-}" == update && "${KERN_APT_MIRROR_PROBED:-}" != yes ]]; then
    select_ubuntu_mirror initial || true
    export KERN_APT_MIRROR_PROBED=yes
  fi
  local command_timeout="$APT_COMMAND_TIMEOUT" attempt refresh=false
  [[ "${1:-}" != update ]] || command_timeout="$APT_UPDATE_COMMAND_TIMEOUT"
  for attempt in 1 2 3 4; do
    if [[ "$refresh" == true ]]; then
      apt_get update || return 1
      refresh=false
    fi
    if apt_get_once "$command_timeout" "$APT_ACQUIRE_RETRIES" "$APT_ACQUIRE_TIMEOUT" "$@"; then
      return 0
    fi
    [[ "$attempt" != 4 ]] || return 1
    echo "APT: command attempt $attempt/4 failed; checking alternative mirrors" >&2
    if select_ubuntu_mirror failover && [[ "${1:-}" != update ]]; then
      refresh=true
    fi
    if [[ "$attempt" != 1 ]]; then sleep $(((attempt - 1) * 5)); fi
  done
  return 1
}
