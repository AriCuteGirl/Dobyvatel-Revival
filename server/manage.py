#!/usr/bin/env python3
"""JSON CLI consumed by Ari's Server Manager."""
import argparse, hashlib, json, os, re, secrets, shutil, tarfile, time
from pathlib import Path

p = argparse.ArgumentParser(add_help=False); p.add_argument('--root', default='.')
ns, ARGS = p.parse_known_args(); ROOT = Path(ns.root).expanduser().resolve()
SERVER = ROOT / 'server' if (ROOT / 'server' / 'dobyvatel_server.py').exists() else ROOT
DATA = SERVER / 'data'; ACCOUNTS = DATA / 'accounts.json'; SETTINGS = SERVER / 'settings.json'; BACKUPS = ROOT / 'backups'

def load(path, default):
    try: return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError: return default

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True); tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding='utf-8')
    os.chmod(tmp, 0o600); tmp.replace(path)

def output(value, code=0): print(json.dumps(value, separators=(',', ':'))); raise SystemExit(code)
def account_db(): return load(ACCOUNTS, {'next_id': 1, 'accounts': {}})
def public_player(a):
    settings = load(SETTINGS, {})
    xp_per_tier = max(1, int(settings.get('season_xp_per_tier', 100)))
    season_tiers = max(1, int(settings.get('season_tiers', 20)))
    season_xp = max(0, int(a.get('season_xp', 0)))
    return {'id': str(a.get('id', '')), 'name': a.get('username', ''), 'level': int(a.get('level', 1)),
            'gold': int(a.get('gold', 1000)), 'lastLogin': int(a.get('last_login', 0)),
            'guest': bool(a.get('guest', False)), 'friendCode': str(a.get('friend_code', '')),
            'friendCount': len(a.get('friends', [])) if isinstance(a.get('friends'), list) else 0,
            'role': str(a.get('role', 'puppy' if str(a.get('username', '')).casefold() == 'ari' else 'member')),
            'buildingsUnlocked': 1 if a.get('buildings_unlocked', True) else 0,
            'castleLevel': int(a.get('castle_level', 7)),
            'energy': int(a.get('energy', 99)), 'lives': int(a.get('lives', 99)),
            'powerups': int(a.get('powerups', 99)),
            'seasonXP': season_xp, 'seasonTier': min(season_tiers, season_xp // xp_per_tier)}
def players(db): return sorted((public_player(a) for a in db.get('accounts', {}).values()), key=lambda x: int(x['id'] or 0))
def settings_schema(): return {
    'server_name': (str, None, None), 'start_gold': (int, 0, 1_000_000),
    'max_players_per_room': (int, 2, 3), 'season_enabled': (bool, None, None),
    'season_id': (str, None, None), 'season_name': (str, None, None),
    'season_xp_per_game': (int, 0, 10000),
    'season_xp_per_tier': (int, 1, 100000), 'season_tiers': (int, 1, 100)}

def main():
    if not ARGS: output({'ok': False, 'error': 'missing command'}, 1)
    cmd = ARGS[0]; db = account_db(); plist = players(db)
    if cmd == 'info': output({'ok': True, 'game': 'dobyvatel', 'version': '0.2.0', 'players': len(plist), 'data_dir': str(DATA)})
    if cmd == 'players': output({'ok': True, 'players': plist})
    if cmd == 'player' and len(ARGS) == 2:
        found = next((x for x in plist if x['id'] == ARGS[1]), None)
        if found is None: output({'ok': False, 'error': 'unknown player'}, 1)
        output({'ok': True, 'player': found})
    if cmd == 'set' and len(ARGS) == 4:
        pid, field, raw = ARGS[1:]; limits = {
            'gold': (0, 1_000_000), 'level': (1, 200),
            'buildings_unlocked': (0, 1), 'castle_level': (1, 7),
            'energy': (0, 9999), 'lives': (0, 9999), 'powerups': (0, 9999),
            'season_xp': (0, 10_000_000)}
        if field not in limits: output({'ok': False, 'error': 'unknown editable field'}, 1)
        value = int(raw); lo, hi = limits[field]
        if not lo <= value <= hi: output({'ok': False, 'error': f'value must be {lo}..{hi}'}, 1)
        found = next((a for a in db.get('accounts', {}).values() if str(a.get('id')) == pid), None)
        if found is None: output({'ok': False, 'error': 'unknown player'}, 1)
        old = found.get(field, 1000 if field == 'gold' else 1)
        found[field] = bool(value) if field == 'buildings_unlocked' else value
        save(ACCOUNTS, db); output({'ok': True, 'old': old, 'new': value})
    if cmd == 'role' and len(ARGS) == 3:
        pid, role = ARGS[1], ARGS[2].lower()
        if role not in ('puppy', 'admin', 'member'):
            output({'ok': False, 'error': 'role must be puppy, admin, or member'}, 1)
        found = next((a for a in db.get('accounts', {}).values() if str(a.get('id')) == pid), None)
        if found is None: output({'ok': False, 'error': 'unknown player'}, 1)
        old = str(found.get('role', 'member')); found['role'] = role
        save(ACCOUNTS, db); output({'ok': True, 'old': old, 'new': role})
    if cmd == 'rename' and len(ARGS) == 3:
        pid, new_name = ARGS[1], ARGS[2].strip()
        if not re.fullmatch(r'[A-Za-z0-9 _.-]{3,24}', new_name):
            output({'ok': False, 'error': 'name must be 3-24 letters, numbers, spaces, dots, dashes, or underscores'}, 1)
        accounts = db.get('accounts', {})
        found_key = next((k for k, a in accounts.items() if str(a.get('id')) == pid), None)
        if found_key is None: output({'ok': False, 'error': 'unknown player'}, 1)
        new_key = new_name.casefold()
        if new_key != found_key and new_key in accounts:
            output({'ok': False, 'error': 'that player name is already used'}, 1)
        account = accounts.pop(found_key); old = str(account.get('username', ''))
        account['username'] = new_name; accounts[new_key] = account
        save(ACCOUNTS, db); output({'ok': True, 'old': old, 'new': new_name})
    if cmd == 'password' and len(ARGS) == 3:
        pid, password = ARGS[1], ARGS[2]
        if not 4 <= len(password) <= 128: output({'ok': False, 'error': 'password must be 4-128 characters'}, 1)
        found = next((a for a in db.get('accounts', {}).values() if str(a.get('id')) == pid), None)
        if found is None: output({'ok': False, 'error': 'unknown player'}, 1)
        salt = secrets.token_bytes(16)
        found['salt'] = salt.hex()
        found['password_hash'] = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 240_000).hex()
        found['guest'] = False
        save(ACCOUNTS, db); output({'ok': True})
    if cmd == 'reset' and len(ARGS) == 2:
        found_key = next((k for k, a in db.get('accounts', {}).items() if str(a.get('id')) == ARGS[1]), None)
        if found_key is None: output({'ok': False, 'error': 'unknown player'}, 1)
        old = db['accounts'].pop(found_key); DATA.mkdir(parents=True, exist_ok=True)
        save(DATA / f'reset-player-{ARGS[1]}-{int(time.time())}.json', old); save(ACCOUNTS, db); output({'ok': True})
    if cmd == 'settings' and len(ARGS) == 1: output({'ok': True, 'settings': load(SETTINGS, {})})
    if cmd == 'settings' and len(ARGS) == 4 and ARGS[1] == 'set':
        key, value = ARGS[2], json.loads(ARGS[3]); schema = settings_schema()
        if key not in schema: output({'ok': False, 'error': 'unknown setting'}, 1)
        typ, lo, hi = schema[key]
        if typ is int and (isinstance(value, bool) or not isinstance(value, int)): output({'ok': False, 'error': 'setting must be an integer'}, 1)
        if typ is str and not isinstance(value, str): output({'ok': False, 'error': 'setting must be text'}, 1)
        if typ is bool and not isinstance(value, bool): output({'ok': False, 'error': 'setting must be on or off'}, 1)
        if typ is str and not 1 <= len(value.strip()) <= 60: output({'ok': False, 'error': 'text must be 1-60 characters'}, 1)
        if lo is not None and not lo <= value <= hi: output({'ok': False, 'error': f'value must be {lo}..{hi}'}, 1)
        value_map = load(SETTINGS, {}); value_map[key] = value; save(SETTINGS, value_map); output({'ok': True, 'settings': value_map})
    if cmd == 'backup':
        BACKUPS.mkdir(parents=True, exist_ok=True); dest = BACKUPS / f'dobyvatel-data-{time.strftime("%Y%m%d-%H%M%S")}.tar.gz'
        with tarfile.open(dest, 'w:gz') as archive:
            if DATA.exists(): archive.add(DATA, arcname='data')
            if SETTINGS.exists(): archive.add(SETTINGS, arcname='settings.json')
        output({'ok': True, 'file': str(dest), 'name': dest.name, 'size': dest.stat().st_size, 'time': int(dest.stat().st_mtime)})
    if cmd == 'backups':
        items = [{'file': str(x), 'name': x.name, 'size': x.stat().st_size, 'time': int(x.stat().st_mtime)} for x in sorted(BACKUPS.glob('*.tar.gz'), reverse=True)]
        output({'ok': True, 'backups': items})
    if cmd == 'restore' and len(ARGS) == 2:
        src = Path(ARGS[1]).expanduser().resolve()
        if not src.is_file() or src.suffixes[-2:] != ['.tar', '.gz']: output({'ok': False, 'error': 'backup file not found'}, 1)
        kept = ROOT / f'data.before-restore-{int(time.time())}'
        if DATA.exists(): shutil.move(str(DATA), str(kept))
        with tarfile.open(src, 'r:gz') as archive:
            allowed = {'data', 'settings.json'}
            if any(Path(m.name).parts[0] not in allowed or '..' in Path(m.name).parts for m in archive.getmembers()): output({'ok': False, 'error': 'invalid backup contents'}, 1)
            archive.extractall(SERVER, filter='data')
        output({'ok': True, 'kept': str(kept) if kept.exists() else ''})
    if cmd == 'logs':
        count = min(1000, max(1, int(ARGS[1]) if len(ARGS) > 1 else 200)); log = SERVER / 'dobyvatel.log'
        lines = log.read_text(errors='replace').splitlines()[-count:] if log.exists() else []; output({'ok': True, 'lines': lines})
    output({'ok': False, 'error': 'unsupported command'}, 1)
if __name__ == '__main__': main()
