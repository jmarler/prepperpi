#!/usr/bin/env bash
# images/build.sh — build a PrepperPi SD image using pi-gen in Docker.
#
# Designed to run natively on ARM64 hosts (Apple Silicon Mac, a Pi,
# GitHub's ubuntu-24.04-arm runners) where Docker pulls the arm64
# Debian base image, no qemu emulation needed. It will also run on
# x86_64 via qemu-user-static -- just slower.
#
# Usage:
#   images/build.sh
#
# Environment overrides:
#   PI_GEN_REF        git ref of RPi-Distro/pi-gen to use (default: master)
#   PREPPERPI_WORK    local build work dir (default: images/.work)
#   PREPPERPI_OUT     local artifact dir   (default: images/out)
#
# Output:
#   images/out/prepperpi-<version>-<date>-prepperpi.zip
#   images/out/prepperpi-<version>-<date>-prepperpi.zip.sha256
#   images/out/prepperpi-<version>-<date>-prepperpi.rpi-imager.json
#     (manifest sidecar for `rpi-imager --repo`; restores the
#      "Use OS customization" button when loading the image).

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly WORK_DIR="${PREPPERPI_WORK:-${SCRIPT_DIR}/.work}"
readonly OUT_DIR="${PREPPERPI_OUT:-${SCRIPT_DIR}/out}"
readonly PI_GEN_REPO="https://github.com/RPi-Distro/pi-gen"
# pi-gen's `arm64` branch produces 64-bit Pi OS Lite images for Pi 3B+
# onwards. The `master` branch is 32-bit armhf, which is NOT what we
# want (Pi 4B+ with 4 GB+ RAM benefits from arm64).
#
# Pinned to a specific commit on the arm64 branch for reproducible
# release builds. Bump deliberately when picking up upstream pi-gen
# fixes; release notes for any bump should call out what changed.
readonly PI_GEN_DEFAULT_REF="4ad56cc850fa60adcc7f07dc15879bc95cc1d281"
readonly PI_GEN_REF="${PI_GEN_REF:-${PI_GEN_DEFAULT_REF}}"
readonly PI_GEN_DIR="${WORK_DIR}/pi-gen"

log() { printf '[prepperpi/image] %s\n' "$*"; }
die() { printf '[prepperpi/image] FATAL: %s\n' "$*" >&2; exit 1; }

check_docker() {
  command -v docker >/dev/null || die "docker not found in PATH"
  docker info >/dev/null 2>&1 || die "docker daemon not reachable (is Docker Desktop running?)"
  # Native arm64 containers on arm64 hosts avoid qemu. Warn (don't
  # fail) on x86_64 so contributors without arm64 still get a build.
  local host_arch
  host_arch=$(uname -m)
  case "$host_arch" in
    arm64|aarch64)
      log "host is ${host_arch}; pi-gen will build natively (fast)" ;;
    x86_64|amd64)
      log "host is ${host_arch}; pi-gen will use qemu-user-static (slow; expect 45-90 min)" ;;
    *)
      log "host arch ${host_arch} is unexpected; build may fail" ;;
  esac
}

clone_pi_gen() {
  # PI_GEN_REF can be a branch name (e.g. `arm64`), a tag, or a full
  # commit SHA. Branches/tags clone shallow via `--branch`; SHAs need a
  # full clone followed by an explicit checkout because git refuses to
  # `--branch <sha>`.
  local is_sha=0
  if [[ "$PI_GEN_REF" =~ ^[0-9a-f]{40}$ ]]; then
    is_sha=1
  fi

  if [[ -d "${PI_GEN_DIR}/.git" ]]; then
    local current
    if (( is_sha )); then
      current=$(git -C "$PI_GEN_DIR" rev-parse HEAD 2>/dev/null || echo "")
    else
      current=$(git -C "$PI_GEN_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
    fi
    if [[ "$current" == "$PI_GEN_REF" ]]; then
      log "reusing existing pi-gen clone at ${PI_GEN_DIR} (on ${PI_GEN_REF})"
      return 0
    fi
    log "existing pi-gen clone is on '${current}', need '${PI_GEN_REF}'; re-cloning"
    rm -rf "$PI_GEN_DIR"
  fi

  log "cloning pi-gen (ref=${PI_GEN_REF}) to ${PI_GEN_DIR}"
  mkdir -p "$WORK_DIR"
  if (( is_sha )); then
    git clone "$PI_GEN_REPO" "$PI_GEN_DIR"
    git -C "$PI_GEN_DIR" checkout --quiet "$PI_GEN_REF"
  else
    git clone --depth 1 --branch "$PI_GEN_REF" "$PI_GEN_REPO" "$PI_GEN_DIR"
  fi
}

stage_prepperpi() {
  local version commit pigen_rev
  version=$(git -C "$REPO_DIR" describe --tags --always --dirty 2>/dev/null || echo 'unknown')
  commit=$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo 'unknown')
  # Resolved (40-char) pi-gen SHA. We always record the SHA, even when
  # PI_GEN_REF was a branch name on the host build path, so a downstream
  # reader can identify the exact upstream code from the image.
  pigen_rev=$(git -C "$PI_GEN_DIR" rev-parse HEAD 2>/dev/null || echo 'unknown')

  log "staging PrepperPi customization into pi-gen (version=${version}, pi-gen=${pigen_rev:0:12})"

  # Copy our stage directory into pi-gen.
  rm -rf "${PI_GEN_DIR}/stage-prepperpi"
  cp -a "${SCRIPT_DIR}/stage-prepperpi" "${PI_GEN_DIR}/stage-prepperpi"

  # Copy the PrepperPi repo into pi-gen so the stage scripts can reach
  # it via the /pi-gen mount point inside the build container.
  rm -rf "${PI_GEN_DIR}/prepperpi-src"
  mkdir -p "${PI_GEN_DIR}/prepperpi-src"
  rsync -a \
    --exclude='.git' \
    --exclude='images/.work' \
    --exclude='images/out' \
    "${REPO_DIR}/" "${PI_GEN_DIR}/prepperpi-src/"

  # Generate the final pi-gen config by concatenating our template
  # with the per-build env vars (pi-gen sources this file so exports
  # survive into the stage scripts).
  {
    cat "${SCRIPT_DIR}/config"
    echo
    echo "# -- generated by images/build.sh --"
    printf 'export PREPPERPI_REPO="/pi-gen/prepperpi-src"\n'
    printf 'export PREPPERPI_VERSION="%s"\n'  "$version"
    printf 'export PREPPERPI_COMMIT="%s"\n'   "$commit"
    printf 'export PREPPERPI_PIGEN_REV="%s"\n' "$pigen_rev"
  } > "${PI_GEN_DIR}/config"

  # Skip the desktop stages (stage3, stage4, stage5) -- we only want
  # stage0-2 (lite base) + our stage. The SKIP and SKIP_IMAGES flag
  # files tell pi-gen to not bother running or exporting those.
  local s
  for s in stage3 stage4 stage5; do
    if [[ -d "${PI_GEN_DIR}/${s}" ]]; then
      touch "${PI_GEN_DIR}/${s}/SKIP" "${PI_GEN_DIR}/${s}/SKIP_IMAGES"
    fi
  done

  # Drop our pi-gen patches into the right sub-steps of pi-gen's own
  # stages. Today it's just one file that blocks apt-listchanges (see
  # the patch header for the Docker Desktop DNS rationale).
  #
  # Pi-gen's sub-stage runner ONLY executes files matching very
  # specific name conventions: `NN-run.sh` (outside chroot),
  # `NN-run-chroot.sh` (inside chroot), `NN-packages`, `NN-patches`,
  # etc. Files with other names are silently ignored. Our patch source
  # has a descriptive filename for self-documentation in this repo,
  # but we install it as `02-run.sh` so pi-gen will actually execute
  # it (after stage0/00-configure-apt's own 00-run.sh and 01-packages).
  local patch_src="${SCRIPT_DIR}/pi-gen-patches/02-no-listchanges.sh"
  local patch_dst="${PI_GEN_DIR}/stage0/00-configure-apt/02-run.sh"
  install -m 0755 "$patch_src" "$patch_dst"
  log "patched pi-gen stage0: ${patch_dst##${PI_GEN_DIR}/}"
}

run_build() {
  # Nuke any stale per-stage output from a previous (possibly failed)
  # run. pi-gen is not incremental; a partially-bootstrapped rootfs
  # left under pi-gen/work/ will confuse debootstrap on the next run
  # ("rmdir: Directory not empty") and sometimes cause it to fall back
  # to fetching Release instead of InRelease, which then fails too.
  # Set PREPPERPI_KEEP_WORK=1 to skip this cleanup during debugging.
  if [[ -d "${PI_GEN_DIR}/work" && "${PREPPERPI_KEEP_WORK:-0}" != "1" ]]; then
    log "clearing stale pi-gen work dir (set PREPPERPI_KEEP_WORK=1 to preserve)"
    rm -rf "${PI_GEN_DIR}/work"
  fi

  log "starting pi-gen build-docker.sh (this is the slow part)"
  cd "$PI_GEN_DIR"
  # build-docker.sh reads the config we wrote above and mounts the
  # pi-gen checkout at /pi-gen inside the build container.
  ./build-docker.sh
}

# Portable SHA-256 over a file. macOS has `shasum` in its base image,
# Linux has `sha256sum`; the build-host wrapper runs on both.
sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    die "neither sha256sum nor shasum found on host"
  fi
}

collect_output() {
  mkdir -p "$OUT_DIR"
  # pi-gen's arm64 branch packs its final artifact as a .zip containing
  # the raw .img (Raspberry Pi Imager reads zipped images natively).
  # It writes TWO images: a "-lite.zip" from stage2 (the base Pi OS
  # Lite) and a "-prepperpi.zip" from our stage (base + PrepperPi
  # customization). Match our stage's suffix exactly so the -lite one
  # never wins -- an earlier `*prepperpi*.zip` glob could pick either.
  local img
  img=$(find "${PI_GEN_DIR}/deploy" -maxdepth 2 -name '*-prepperpi.zip' -type f 2>/dev/null | sort | tail -n1) || true
  if [[ -z "${img:-}" ]]; then
    # Fall back to .img.xz in case a future pi-gen version switches
    # back to that format.
    img=$(find "${PI_GEN_DIR}/deploy" -maxdepth 2 -name '*-prepperpi.img.xz' -type f 2>/dev/null | sort | tail -n1) || true
  fi
  [[ -n "${img:-}" ]] || die "no prepperpi image found under ${PI_GEN_DIR}/deploy/"

  local base
  base=$(basename "$img")
  cp "$img" "${OUT_DIR}/${base}"
  ZIP_PATH="${OUT_DIR}/${base}"
  (cd "$OUT_DIR" && sha256_file "$base" > "${base}.sha256")

  log "artifact: ${ZIP_PATH}"
  log "sha256:   ${ZIP_PATH}.sha256"
  ls -lh "${ZIP_PATH}"
}

write_rpi_imager_manifest() {
  # Pi Imager 2.x greys out the "Use OS customization" button for any
  # locally-loaded image because it has no way to know the image's
  # init_format. Shipping a JSON sidecar manifest (the same schema
  # Imager uses for its online OS list) lets the operator load our
  # image via `rpi-imager --repo <manifest>` and get customization back.
  #
  # init_format MUST match what the image can actually process on first
  # boot. Our image is stage2 (Pi OS Lite) Trixie-based, which ships
  # cloud-init + raspberrypi-sys-mods; the matching format is
  # "cloudinit-rpi" (confirmed against Imager's live OS list at
  # https://downloads.raspberrypi.org/os_list_imagingutility_v4.json).
  [[ -n "${ZIP_PATH:-}" ]] || die "write_rpi_imager_manifest called before collect_output"

  local base
  base=$(basename "$ZIP_PATH")
  local manifest_path="${OUT_DIR}/${base%.zip}.rpi-imager.json"

  log "decompressing image to compute extract_size + extract_sha256"
  local tmp_img
  tmp_img=$(mktemp -t prepperpi-img.XXXXXX)
  # shellcheck disable=SC2064
  trap "rm -f '$tmp_img'" RETURN
  unzip -p "$ZIP_PATH" >"$tmp_img"

  local image_download_size image_download_sha256 extract_size extract_sha256
  image_download_size=$(wc -c <"$ZIP_PATH" | tr -d ' ')
  image_download_sha256=$(sha256_file "$ZIP_PATH")
  extract_size=$(wc -c <"$tmp_img" | tr -d ' ')
  extract_sha256=$(sha256_file "$tmp_img")
  rm -f "$tmp_img"

  local release_date
  release_date=$(date -u +%Y-%m-%d)

  cat >"$manifest_path" <<EOF
{
  "os_list": [
    {
      "name": "PrepperPi",
      "description": "PrepperPi offline reference appliance (Raspberry Pi OS Lite arm64 + prepperpi stack).",
      "url": "file://${ZIP_PATH}",
      "release_date": "${release_date}",
      "image_download_size": ${image_download_size},
      "image_download_sha256": "${image_download_sha256}",
      "extract_size": ${extract_size},
      "extract_sha256": "${extract_sha256}",
      "init_format": "cloudinit-rpi",
      "devices": ["pi5-64bit", "pi4-64bit", "pi3-64bit"]
    }
  ]
}
EOF
  log "manifest: ${manifest_path}"
  log "flash with: rpi-imager --repo 'file://${manifest_path}'"
}

main() {
  check_docker
  clone_pi_gen
  stage_prepperpi
  run_build
  collect_output
  write_rpi_imager_manifest
  log "done"
}

# Only run main when executed directly. Sourcing is supported so tests
# (or a manifest-only rerun against an existing build) can pull in the
# helper functions without triggering a full Docker build.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
