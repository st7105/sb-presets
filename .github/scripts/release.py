import argparse
import base64
import hashlib
import json
import os
import subprocess
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


BUNDLE_DIR = Path("dist")
BUNDLE_PREFIX = "sb-presets"
BUNDLE_INFO_PATH = BUNDLE_DIR / "bundle-info.json"


def run(args: list[str], capture: bool = False) -> str:
    result = subprocess.run(
        args,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
    )
    return result.stdout if capture else ""


def append_github_env(name: str, value: str) -> None:
    github_env = os.environ.get("GITHUB_ENV")
    if not github_env:
        print(f"{name}={value}")
        return

    with open(github_env, "a", encoding="utf-8") as file:
        file.write(f"{name}={value}\n")


def prepare_release(build: str | None) -> None:
    build_number = int(build or git_commit_count())
    if build_number <= 0:
        raise SystemExit("Build number must be positive.")

    tag = f"bundle-{build_number}"
    notes = release_notes(build_number)
    Path("release-notes.md").write_text(notes + "\n", encoding="utf-8")

    append_github_env("BUILD", str(build_number))
    append_github_env("TAG", tag)
    append_github_env("RELEASE_NAME", f"{BUNDLE_PREFIX} build {build_number}")


def release_notes(build_number: int) -> str:
    commit = git_commit()
    message = git_message()
    return "\n".join(
        [
            f"Preset bundle build `{build_number}`.",
            "",
            f"- Commit: `{commit}`",
            f"- Message: {message}",
        ]
    )


def validate_assets() -> None:
    preset_files = sorted(Path("assets/presets").glob("*.toml"))
    logo_files = sorted(Path("assets/logos").glob("*.png"))
    if not preset_files:
        raise SystemExit("No preset TOML files found in assets/presets.")

    for path in preset_files:
        with path.open("rb") as file:
            tomllib.load(file)

    for path in logo_files:
        if not path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
            raise SystemExit(f"Logo is not a PNG file: {path}")


def build_bundle() -> None:
    build_number = required_build()

    preset_files = sorted(
        Path("assets/presets").glob("*.toml"), key=lambda path: path.name.casefold()
    )
    logo_files = sorted(
        Path("assets/logos").glob("*.png"), key=lambda path: path.name.casefold()
    )
    if not preset_files:
        raise SystemExit("No preset TOML files found in assets/presets.")

    BUNDLE_DIR.mkdir(exist_ok=True)
    bundle_name = f"{BUNDLE_PREFIX}-{build_number}.zip"
    bundle_path = BUNDLE_DIR / bundle_name

    manifest = {
        "schema_version": 1,
        "build": build_number,
        "commit": git_commit(),
        "preset_count": len(preset_files),
        "logo_count": len(logo_files),
        "presets": [path.name for path in preset_files],
        "logos": [path.name for path in logo_files],
    }

    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        for path in preset_files:
            archive.write(path, f"presets/{path.name}")
        for path in logo_files:
            archive.write(path, f"logos/{path.name}")

    sha256 = sha256_file(bundle_path)
    bundle_info = {
        "bundle_name": bundle_name,
        "bundle_path": str(bundle_path).replace("\\", "/"),
        "sha256": sha256,
        "preset_count": len(preset_files),
        "logo_count": len(logo_files),
    }
    BUNDLE_INFO_PATH.write_text(
        json.dumps(bundle_info, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    append_github_env("BUNDLE_NAME", bundle_name)
    append_github_env("BUNDLE_PATH", bundle_info["bundle_path"])
    append_github_env("BUNDLE_SHA256", sha256)
    append_github_env("PRESET_COUNT", str(len(preset_files)))
    append_github_env("LOGO_COUNT", str(len(logo_files)))


def write_update_manifest(public_base: str) -> None:
    build_number = required_build()
    bundle_info = read_bundle_info()
    bundle_name = os.environ.get("BUNDLE_NAME", bundle_info["bundle_name"])
    base = public_base.rstrip("/")

    manifest = {
        "build": build_number,
        "bundle": bundle_name,
        "url": f"{base}/sb-presets/{bundle_name}",
        "sha256": os.environ.get("BUNDLE_SHA256", bundle_info["sha256"]),
        "preset_count": int(os.environ.get("PRESET_COUNT", bundle_info["preset_count"])),
        "logo_count": int(os.environ.get("LOGO_COUNT", bundle_info["logo_count"])),
        "commit": git_commit(),
    }
    Path("latest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def upload_webdav(remote_dir: str, files: list[str]) -> None:
    base_url = required_env("WEBDAV_BASE_URL").rstrip("/")
    user = required_env("WEBDAV_USER")
    password = required_env("WEBDAV_PASSWORD")
    remote_dir = remote_dir.strip("/")

    collection_url = webdav_url(base_url, remote_dir) + "/"
    try:
        webdav_request("MKCOL", collection_url, user, password)
    except urllib.error.HTTPError as error:
        print(f"MKCOL {collection_url} returned HTTP {error.code}; continuing.")

    for file_arg in files:
        local, remote_name = parse_upload_file(file_arg)
        target_url = webdav_url(base_url, remote_dir, remote_name)
        webdav_request("PUT", target_url, user, password, local.read_bytes())
        print(f"Uploaded {local} to {target_url}")


def parse_upload_file(value: str) -> tuple[Path, str]:
    if "=" not in value:
        raise SystemExit("--file must use local=remote syntax.")

    local_text, remote_name = value.split("=", 1)
    local = Path(local_text)
    if not local.is_file():
        raise SystemExit(f"Upload source does not exist: {local}")
    if not remote_name.strip():
        raise SystemExit("--file remote name must not be empty.")
    return local, remote_name.strip()


def webdav_url(base_url: str, *parts: str) -> str:
    encoded = [
        urllib.parse.quote(part.strip("/"), safe="")
        for part in parts
        if part.strip("/")
    ]
    return "/".join([base_url, *encoded])


def webdav_request(
    method: str,
    url: str,
    user: str,
    password: str,
    data: bytes | None = None,
) -> None:
    request = urllib.request.Request(url, data=data, method=method)
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    request.add_header("Authorization", f"Basic {token}")
    if data is not None:
        request.add_header("Content-Type", "application/octet-stream")

    with urllib.request.urlopen(request) as response:
        response.read()


def read_bundle_info() -> dict[str, str | int]:
    if not BUNDLE_INFO_PATH.exists():
        raise SystemExit(
            "Bundle metadata is missing. Run build-bundle before write-update-manifest."
        )
    return json.loads(BUNDLE_INFO_PATH.read_text(encoding="utf-8"))


def required_build() -> int:
    build = os.environ.get("BUILD")
    if not build:
        raise SystemExit("BUILD is required. Run prepare first.")
    return int(build)


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} environment variable is required.")
    return value


def git_commit_count() -> str:
    return run(["git", "rev-list", "--count", "HEAD"], capture=True).strip()


def git_commit() -> str:
    return run(["git", "rev-parse", "HEAD"], capture=True).strip()


def git_message() -> str:
    return run(["git", "log", "-1", "--pretty=%s"], capture=True).strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="sb-presets release helper.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--build")

    subparsers.add_parser("validate-assets")
    subparsers.add_parser("build-bundle")

    update_manifest = subparsers.add_parser("write-update-manifest")
    update_manifest.add_argument("--public-base", required=True)

    upload = subparsers.add_parser("upload-webdav")
    upload.add_argument("--remote-dir", required=True)
    upload.add_argument("--file", action="append", required=True)

    args = parser.parse_args()

    if args.command == "prepare":
        prepare_release(args.build)
    elif args.command == "validate-assets":
        validate_assets()
    elif args.command == "build-bundle":
        build_bundle()
    elif args.command == "write-update-manifest":
        write_update_manifest(args.public_base)
    elif args.command == "upload-webdav":
        upload_webdav(args.remote_dir, args.file)


if __name__ == "__main__":
    main()
