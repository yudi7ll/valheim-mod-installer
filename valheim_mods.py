#!/usr/bin/env python3
"""Small headless Valheim BepInEx mod updater (Thunderstore + Hexium)."""
import argparse
import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

SOURCES = {
    'thunderstore': 'https://thunderstore.io/c/valheim/api/v1/package/',
    'hexium': 'https://valheim.hexium.gg/api/v1/package/',
}
VERSION = re.compile(r'^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$')
MAX_ZIP = 256 * 1024 * 1024
BEPINEX_PACKAGE = 'denikson-BepInExPack_Valheim'  # Gale's mod loader package for Valheim


def fetch(url, limit=128 * 1024 * 1024):
    req = urllib.request.Request(url, headers={'User-Agent': 'valheim-dual-mods/0.1'})
    with urllib.request.urlopen(req, timeout=90) as response:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f'Download too large: {url}')
        return data


def version_key(v):
    # Stable versions beat prereleases of the same numeric version.
    main, _, suffix = v.partition('-')
    nums = tuple(int(p) for p in main.split('+', 1)[0].split('.'))
    return (nums, not bool(suffix), suffix)


def index_packages():
    out = {}
    for source, url in SOURCES.items():
        print(f'Fetching {source} index...', flush=True)
        try:
            packages = json.loads(fetch(url, MAX_ZIP))
            if not isinstance(packages, list):
                raise ValueError('Expected list of packages')
            out[source] = {p['full_name'].lower(): p for p in packages if p.get('full_name') and p.get('versions')}
        except Exception as e:
            print(f'WARNING: {source} unavailable: {e}', file=sys.stderr)
    if not out:
        raise RuntimeError('Both mod sources are unavailable')
    return out


def resolve(index, fullname, preference, minimum=None):
    candidates = []
    for source, packages in index.items():
        package = packages.get(fullname.lower())
        if package:
            for v in package.get('versions', []):
                number = v.get('version_number', '')
                if VERSION.fullmatch(number) and v.get('download_url'):
                    if minimum and version_key(number) < version_key(minimum):
                        continue
                    candidates.append((source, package, v))
    if not candidates:
        raise LookupError(f'No suitable version found for {fullname} (minimum={minimum})')
    # Prefer higher version; break ties using configured source.
    return max(candidates, key=lambda x: (version_key(x[2]['version_number']), x[0] == preference))


def mod_requirements(version):
    for item in version.get('dependencies', []):
        # Dependency is Team-Package-Major.Minor.Patch, not necessarily the newest.
        match = re.fullmatch(r'(.+)-(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)', item)
        if not match:
            raise ValueError(f'Invalid dependency: {item!r}')
        name, minimum = match.groups()
        if 'bepinexpack' in name.lower():
            continue  # BepInEx is handled separately by install_bepinex_zip().
        yield name, minimum


def is_modpack(package, version):
    """Return whether a Thunderstore-style package is a modpack."""
    categories = package.get('categories', []) or version.get('categories', [])
    return any(isinstance(category, str) and category.lower() == 'modpacks'
               for category in categories)


def plan_mods(index, mods, preference):
    required = {m.lower(): (m, None) for m in mods}
    selected = {}
    for _ in range(1000):
        changes = False
        for key, (name, minimum) in list(required.items()):
            source, package, version = resolve(index, name, preference, minimum)
            candidate = (source, package, version)
            if selected.get(key) != candidate:
                selected[key] = candidate
                changes = True
            for dep_name, dep_min in mod_requirements(version):
                dep_key = dep_name.lower()
                old = required.get(dep_key)
                if not old or (old[1] and version_key(dep_min) > version_key(old[1])):
                    required[dep_key] = (dep_name, dep_min)
                    changes = True
        if not changes:
            return selected
    raise RuntimeError('Dependency resolution did not converge')


def install_zip(data, target, dest_id):
    # Only install DLLs/assets into a package-owned plugin directory.
    # Keep config files on upgrade (copy only if missing).
    plugin_dir = target / 'BepInEx' / 'plugins' / dest_id
    staging = Path(tempfile.mkdtemp(prefix='.mod-stage-', dir=plugin_dir.parent))
    configs = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_ZIP:
                raise ValueError('Uncompressed archive too large')
            for item in z.infolist():
                path = PurePosixPath(item.filename.replace('\\', '/'))
                parts = path.parts
                if item.is_dir():
                    continue
                if any(x in ('..', '.') for x in parts) or not parts or path.is_absolute():
                    raise ValueError(f'Unsafe archive path: {path}')
                if item.file_size > MAX_ZIP:
                    raise ValueError('Oversized archive member')
                lower = [p.lower() for p in parts]
                if 'config' in lower and 'bepinex' in lower:
                    pos = lower.index('config')
                    relative = Path(*parts[pos + 1:])
                    if relative.parts:
                        configs.append((relative, z.read(item)))
                    continue
                if 'patchers' in lower and 'bepinex' in lower:
                    raise ValueError('Package contains BepInEx patchers: requires manual installation')
                if 'plugins' in lower and 'bepinex' in lower:
                    pos = lower.index('plugins')
                    relative = Path(*parts[pos + 1:])
                elif lower[0] == 'plugins':
                    relative = Path(*parts[1:])
                elif lower[0] in ('manifest.json', 'readme.md', 'changelog.md', 'icon.png'):
                    continue
                elif lower[0] in ('bepinex', 'doorstop_libs') or lower[0].endswith(('.so', '.sh', '.exe')):
                    raise ValueError('Archive includes loader/root-level files: install manually')
                else:
                    # Standard Thunderstore root-layout plugin package.
                    relative = Path(*parts)
                if not relative.parts:
                    continue
                dest = staging / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(item) as src, dest.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
        if not any(staging.rglob('*.dll')):
            raise ValueError('No DLL found; unsupported package layout')
        # Replace only the directory owned by this script. Roll back on failure.
        old = plugin_dir.with_name(plugin_dir.name + '.old-updater')
        if old.exists():
            raise RuntimeError(f'Unexpected backup directory: {old}')
        if plugin_dir.exists():
            plugin_dir.rename(old)
        try:
            staging.rename(plugin_dir)
        except Exception:
            if old.exists():
                old.rename(plugin_dir)
            raise
        if old.exists():
            shutil.rmtree(old)
        config_dir = target / 'BepInEx' / 'config'
        for relative, content in configs:
            dst = config_dir / relative
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                dst.write_bytes(content)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def install_bepinex_zip(data, target):
    """Extract a BepInExPack archive into the game root.

    Mirrors Gale's BepinexInstaller logic:
    - Strips the single top-level directory (e.g. BepInExPack_Valheim/).
    - Skips top-level-only entries (manifest.json, icon.png, etc.).
    - Copy-once for BepInEx/config/ files (preserves user edits on upgrade).
    - Overwrites everything else.
    - Makes .sh scripts executable on POSIX systems.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if sum(i.file_size for i in z.infolist()) > MAX_ZIP:
            raise ValueError('BepInEx archive uncompressed size exceeds limit')

        entries = [i for i in z.infolist() if not i.is_dir()]
        if not entries:
            raise ValueError('BepInEx archive contains no files')

        # Collect all top-level directories present in the archive.
        top_dirs = {
            PurePosixPath(e.filename.replace('\\', '/')).parts[0]
            for e in entries
            if len(PurePosixPath(e.filename.replace('\\', '/')).parts) > 1
        }
        if len(top_dirs) != 1:
            raise ValueError(
                f'Expected exactly one top-level directory in BepInEx archive, found: {top_dirs}'
            )

        for item in entries:
            path = PurePosixPath(item.filename.replace('\\', '/'))
            parts = path.parts
            if any(x in ('..', '.') for x in parts) or path.is_absolute():
                raise ValueError(f'Unsafe archive path: {path}')

            # Top-level-only entries are Thunderstore metadata — skip them.
            if len(parts) == 1:
                continue

            # Strip the single top-level dir.
            relative = Path(*parts[1:])
            if not relative.parts:
                continue

            dest = target / relative

            # BepInEx/config/ files are copy-once: preserve user edits on upgrade.
            lower_parts = [p.lower() for p in relative.parts]
            is_config = 'bepinex' in lower_parts and 'config' in lower_parts
            if is_config and dest.exists():
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(item) as src, dest.open('wb') as dst:
                shutil.copyfileobj(src, dst)

            # Ensure shell scripts are executable (Linux/macOS servers).
            if dest.suffix == '.sh':
                dest.chmod(dest.stat().st_mode | 0o111)


def bepinex_installed_version(state):
    """Return the recorded BepInEx version from the state dict, or None."""
    return state.get('__bepinex__', {}).get('version')


def read_mods(path):
    """Read author/modname entries and return package identifiers."""
    entries = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        entry = line.strip()
        if not entry:
            continue
        if entry.count('/') != 1:
            raise ValueError(f'line {line_number}: expected author/modname, got {entry!r}')
        author, modname = entry.split('/')
        if not author or not modname:
            raise ValueError(f'line {line_number}: author and modname must both be nonempty')
        entries.append(f'{author}-{modname}')
    if not entries:
        raise ValueError('Mods file must contain at least one author/modname entry')
    return entries


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--server', type=Path, required=True, help='Valheim server game root')
    ap.add_argument('--mods', type=Path, required=True,
                    help='Text file with one author/modname entry per line')
    ap.add_argument('--prefer', choices=SOURCES, default='thunderstore')
    ap.add_argument('--apply', dest='apply', action='store_true', default=True,
                    help='Download and install (default; retained for compatibility)')
    ap.add_argument('--dry-run', dest='apply', action='store_false',
                    help='Report changes without downloading or installing')
    args = ap.parse_args()
    target = args.server.resolve()
    try:
        requested = read_mods(args.mods)
    except (OSError, ValueError) as e:
        ap.error(str(e))
    state_path = target / '.valheim-dual-mods.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    index = index_packages()

    # --- BepInEx installation / update ---
    try:
        bep_source, bep_package, bep_version = resolve(index, BEPINEX_PACKAGE, args.prefer)
    except LookupError as e:
        ap.error(str(e))
    bep_ver = bep_version['version_number']
    installed_bep_ver = bepinex_installed_version(state)
    plugins_dir = target / 'BepInEx' / 'plugins'
    needs_bepinex = (
        not plugins_dir.is_dir()
        or not installed_bep_ver
        or version_key(bep_ver) > version_key(installed_bep_ver)
    )
    if needs_bepinex:
        old_ver = installed_bep_ver or 'none'
        print(f'UPDATE  {bep_package["full_name"]} {old_ver} -> {bep_ver} ({bep_source})')
        if args.apply:
            try:
                bep_data = fetch(bep_version['download_url'], MAX_ZIP)
                install_bepinex_zip(bep_data, target)
                plugins_dir.mkdir(parents=True, exist_ok=True)
                state['__bepinex__'] = {'version': bep_ver, 'source': bep_source}
                tmp = state_path.with_suffix('.json.tmp')
                tmp.write_text(json.dumps(state, indent=2) + '\n')
                tmp.replace(state_path)
            except Exception as e:
                raise SystemExit(f'FAILED  BepInEx: {e}')
    else:
        print(f'OK      {bep_package["full_name"]} {bep_ver} ({bep_source})')

    # --- Mod installation / update ---
    selected = plan_mods(index, requested, args.prefer)
    failed = 0
    for key, (source, package, version) in sorted(selected.items()):
        if is_modpack(package, version):
            print(f'SKIP    {package["full_name"]}: modpack dependencies are installed individually')
            continue
        current = state.get(key, {})
        ver = version['version_number']
        if current.get('version') and version_key(current['version']) > version_key(ver):
            print(f'SKIP    {package["full_name"]}: installed {current["version"]} is newer than available {ver}')
            continue
        if current.get('version') == ver and current.get('source') == source:
            print(f'OK      {package["full_name"]} {ver} ({source})')
            continue
        print(f'UPDATE  {package["full_name"]} {current.get("version", "none")} -> {ver} ({source})')
        if not args.apply:
            continue
        try:
            data = fetch(version['download_url'], MAX_ZIP)
            dest_id = re.sub(r'[^A-Za-z0-9_.-]', '_', package['full_name'])
            install_zip(data, target, dest_id)
            state[key] = {'version': ver, 'source': source}
            tmp = state_path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(state, indent=2) + '\n')
            tmp.replace(state_path)
        except Exception as e:
            print(f'FAILED  {package["full_name"]}: {e}', file=sys.stderr)
            failed += 1
    if failed:
        raise SystemExit(f'{failed} mods failed; see errors above')


if __name__ == '__main__':
    main()
