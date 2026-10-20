#!/usr/bin/env python3
"""Minimal protocol emulator for Dobyvatel 2019.04.17.

The legacy AIR client POSTs encrypted frames to /game.  Frames use 4-byte
little-endian `(byte_length << 8) | flags` headers and ChaCha8 (8-byte nonce).
This first milestone intentionally keeps responses on the client's default
key, which lets a local server accept the initial LOGIN without possessing the
retired service's RSA private key.
"""
from __future__ import annotations
import argparse, hashlib, hmac, json, os, re, secrets, struct, threading, time, xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

KEY = bytes(range(1, 33))
MASK = 0xFFFFFFFF
sessions: dict[str, dict[str, str]] = {}
login_tickets: dict[str, dict[str, object]] = {}
web_tokens: dict[str, dict[str, object]] = {}
DATA_DIR = os.environ.get('DOBYVATEL_DATA_DIR', os.path.join(os.path.dirname(__file__), 'data'))
ACCOUNTS_FILE = os.path.join(DATA_DIR, 'accounts.json')
SETTINGS_FILE = os.path.join(os.path.dirname(__file__), 'settings.json')
QUESTIONS_FILE = os.environ.get(
    'DOBYVATEL_QUESTIONS_FILE', os.path.join(os.path.dirname(__file__), 'questions_cz.json'))
# `gfx/maps/cz.swf` exports MARKER1..MARKER15 and Semu.map_areanum is 15.
MAP_AREA_COUNT = 15
# Three areas are occupied by the players' starting castles.  The old value of
# three was only a short smoke-test match and made the production game end after
# three questions.  A normal free-conquest phase fills all twelve neutral areas.
MATCH_ROUNDS = max(1, int(os.environ.get(
    'DOBYVATEL_MATCH_ROUNDS', str(MAP_AREA_COUNT - 3))))
account_lock = threading.RLock()
friendly_lock = threading.RLock()
friendly_rooms: dict[str, dict[str, object]] = {}
next_friendly_room = 1

DEFAULT_SETTINGS: dict[str, object] = {
    'server_name': 'Dobyvatel Revival', 'start_gold': 1000,
    'max_players_per_room': 3, 'season_enabled': False,
    'season_id': 'revival-1', 'season_name': 'Revival Season 1',
    'season_xp_per_game': 25, 'season_xp_per_tier': 100,
    'season_tiers': 20,
}

def _load_questions() -> list[dict[str, object]]:
    try:
        with open(QUESTIONS_FILE, encoding='utf-8') as fh:
            values = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        values = []
    valid = []
    for item in values if isinstance(values, list) else []:
        if (isinstance(item, dict) and item.get('type') == 'tip' and
                str(item.get('question', '')).strip() and
                isinstance(item.get('answer'), int)):
            valid.append(item)
    if valid:
        return valid
    return [{'id': 'fallback-1348', 'type': 'tip', 'category': 4, 'difficulty': 1,
             'question': 'Ve kterém roce byla založena Univerzita Karlova?',
             'answer': 1348}]

def _select_question(game: dict[str, object]) -> dict[str, object]:
    """Choose a supported numeric question without immediate repetition."""
    questions = _load_questions()
    history = game.setdefault('question_history', [])
    if not isinstance(history, list):
        history = game['question_history'] = []
    recent = {str(value) for value in history[-8:]}
    candidates = [q for q in questions if str(q.get('id')) not in recent] or questions
    question = dict(secrets.choice(candidates))
    history.append(str(question.get('id', '')))
    game['question_history'] = history[-8:]
    game['question'] = question
    return question

def _current_question(game: dict[str, object]) -> dict[str, object]:
    question = game.get('question')
    return question if isinstance(question, dict) else _select_question(game)

def _settings() -> dict[str, object]:
    """Read settings on demand so Server Manager changes take effect live."""
    value = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as fh:
            saved = json.load(fh)
        if isinstance(saved, dict): value.update(saved)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return value

def _season_status(account: dict[str, object]) -> dict[str, object]:
    settings = _settings(); season_id = str(settings.get('season_id', 'revival-1'))
    if str(account.get('season_id', '')) != season_id:
        account.update({'season_id': season_id, 'season_xp': 0, 'season_tier': 0,
                        'season_claimed': []})
    xp = max(0, int(account.get('season_xp', 0)))
    per_tier = max(1, int(settings.get('season_xp_per_tier', 100)))
    tiers = max(1, int(settings.get('season_tiers', 20)))
    tier = min(tiers, xp // per_tier); account['season_tier'] = tier
    return {'enabled': False, 'id': season_id,
            'name': str(settings.get('season_name', 'Revival Season 1')),
            'xp': xp, 'tier': tier, 'tiers': tiers, 'xp_per_tier': per_tier,
            'xp_in_tier': 0 if tier >= tiers else xp % per_tier,
            'xp_to_next': 0 if tier >= tiers else per_tier - (xp % per_tier),
            'claimed': list(account.get('season_claimed', []))}

def _season_reward(account: dict[str, object], tier: int) -> dict[str, object]:
    """Grant an automatic, fully-free reward for one newly reached tier."""
    reward: dict[str, object]
    if tier == 20:
        account['buildings_unlocked'] = True; account['castle_level'] = 7
        account['powerups'] = int(account.get('powerups', 0)) + 25
        reward = {'type': 'grand_bundle', 'buildings_unlocked': True,
                  'castle_level': 7, 'hints': 25}
    elif tier % 5 == 0:
        account['castle_level'] = min(7, int(account.get('castle_level', 1)) + 1)
        account['powerups'] = int(account.get('powerups', 0)) + 5
        reward = {'type': 'upgrade', 'castle_level': account['castle_level'], 'hints': 5}
    elif tier % 4 == 0:
        account['lives'] = int(account.get('lives', 0)) + 3
        reward = {'type': 'lives', 'amount': 3}
    elif tier % 3 == 0:
        account['energy'] = int(account.get('energy', 0)) + 10
        reward = {'type': 'energy', 'amount': 10}
    elif tier % 2 == 0:
        account['gold'] = int(account.get('gold', 0)) + 500
        reward = {'type': 'gold', 'amount': 500}
    else:
        account['powerups'] = int(account.get('powerups', 0)) + 3
        reward = {'type': 'hints', 'amount': 3}
    reward['tier'] = tier
    return reward

def _award_season_game(account: dict[str, object]) -> list[dict[str, object]]:
    settings = _settings(); status = _season_status(account)
    if not status['enabled']: return []
    old_tier = int(status['tier'])
    account['season_xp'] = int(status['xp']) + max(0, int(settings.get('season_xp_per_game', 25)))
    new_status = _season_status(account)
    rewards = [_season_reward(account, tier)
               for tier in range(old_tier + 1, int(new_status['tier']) + 1)]
    claimed = account.setdefault('season_claimed', [])
    if not isinstance(claimed, list): claimed = account['season_claimed'] = []
    claimed.extend(rewards)
    account['season_claimed'] = claimed[-max(20, int(new_status['tiers'])):]
    return rewards

def _friendly_members(room: dict[str, object]) -> list[str]:
    return [str(x) for x in room.get('members', []) if str(x) in sessions]

def _friendly_humans(room: dict[str, object]) -> list[str]:
    return _friendly_members(room)

def _friendly_detach(cid: str, *, reopen: bool = True) -> None:
    """Remove a connection from its shared room and return it to the lobby."""
    session = sessions.get(cid)
    if not session: return
    room_id = session.pop('friendly_room', '')
    room = friendly_rooms.get(room_id)
    session.update({'screen': 'WAIT', 'roomsel': '0'})
    mark_dirty(session)
    if not room: return
    members = room.get('members', [])
    if isinstance(members, list) and cid in members: members.remove(cid)
    for slot in room.get('slots', []):
        if isinstance(slot, dict) and slot.get('cid') == cid:
            slot.pop('cid', None)
            slot['kind'] = 'code' if reopen else 'bot'
    _friendly_mark(room)
    _friendly_mark_lobbies()

def _friendly_mark(room: dict[str, object]) -> None:
    room['generation'] = int(room.get('generation', 0)) + 1
    for member in _friendly_members(room):
        mark_dirty(sessions[member])

def _friendly_mark_lobbies() -> None:
    """Wake lobby long-polls when the public room directory changes."""
    for session in sessions.values():
        if session.get('screen') == 'WAIT':
            mark_dirty(session)

def _friendly_slot(room: dict[str, object], cid: str) -> int:
    slots = room.get('slots', [])
    for index, slot in enumerate(slots, 1):
        if isinstance(slot, dict) and slot.get('cid') == cid: return index
    return 0

def _friendly_active_tag(room: dict[str, object]) -> str:
    values: dict[str, object] = {
        'ID': room['id'], 'RULES': room.get('rules', '2'),
        'QCATS': room.get('qcats', ''), 'CODE': room.get('code', ''),
    }
    complete = True
    for index, slot in enumerate(room.get('slots', []), 1):
        assert isinstance(slot, dict)
        cid = str(slot.get('cid', ''))
        kind = str(slot.get('kind', 'open'))
        if cid in sessions:
            values[f'P{index}'] = f"{sessions[cid]['user_id']},1"
            values[f'PN{index}'] = sessions[cid]['name']
        elif kind == 'bot':
            values[f'P{index}'] = '-1,1'; values[f'PN{index}'] = ''
        elif kind == 'code':
            values[f'P{index}'] = '-2,0'; values[f'PN{index}'] = ''
            complete = False
        elif kind == 'invite':
            # The client recognizes a direct invitation by finding its own
            # user id in P2/P3 and then emits CONFIRMSEPROOM.
            values[f'P{index}'] = f"{slot.get('user_id', '0')},0"
            values[f'PN{index}'] = str(slot.get('name', ''))
            complete = False
        else:
            values[f'P{index}'] = '0,0'; values[f'PN{index}'] = ''
            complete = False
        values[f'EXTID{index}'] = ''
    if complete: values['STARTDELAY'] = '1'
    return '<ACTIVESEPROOM ' + attrs(**values) + ' />'

def _friendly_room_tag(room: dict[str, object]) -> str:
    active = ET.fromstring(_friendly_active_tag(room))
    values = dict(active.attrib); values.pop('STARTDELAY', None)
    return '<SEPROOM ' + attrs(**values) + ' />'

def _friendly_game_setup(room: dict[str, object]) -> None:
    game = {'stage': 'prepare', 'ready': set(), 'bases': {}, 'claims': {},
            'answers': {}, 'round': 0, 'winner': 1, 'free_area': 0,
            'game_recorded': False}
    room['game'] = game; room['started'] = True
    for cid in _friendly_members(room):
        sessions[cid].update({'screen': 'MAP_WD', 'game_stage': 'prepare',
                              'listen_count': '0', 'game_recorded': '0'})
    _friendly_mark(room)
    _friendly_mark_lobbies()

def _solo_game_setup(cid: str) -> dict[str, object]:
    """Represent a player-versus-two-bots match with the shared state machine."""
    room: dict[str, object] = {
        'id': 1, 'rules': '2', 'qcats': '', 'code': '', 'owner': cid,
        'members': [cid], 'started': True,
        'slots': [{'kind': 'human', 'cid': cid}, {'kind': 'bot'}, {'kind': 'bot'}],
        'generation': 0,
        'game': {'stage': 'prepare', 'ready': set(), 'bases': {}, 'claims': {},
                 'answers': {}, 'round': 0, 'winner': 1, 'free_area': 0,
                 'game_recorded': False},
    }
    session = sessions[cid]
    session.update({'screen': 'MAP_WD', 'listen_count': '0', 'solo_room': room,
                    'game_recorded': '0'})
    _friendly_mark(room)
    return room

def _active_game_room(cid: str) -> dict[str, object] | None:
    session = sessions.get(cid)
    if not session: return None
    friendly = friendly_rooms.get(str(session.get('friendly_room', '')))
    if friendly and friendly.get('started'): return friendly
    solo = session.get('solo_room')
    return solo if isinstance(solo, dict) and solo.get('started') else None

def _friendly_finish(room: dict[str, object]) -> None:
    """Close a shared match for every connected participant."""
    room_id = str(room.get('id', ''))
    members = list(_friendly_members(room))
    friendly_rooms.pop(room_id, None)
    for member in members:
        session = sessions.get(member)
        if session:
            session.pop('friendly_room', None)
            session.update({'screen': 'WAIT', 'roomsel': '0'})
            mark_dirty(session)
    _friendly_mark_lobbies()

def _friendly_is_bot(room: dict[str, object], slot: int) -> bool:
    slots = room.get('slots', [])
    if not 1 <= slot <= len(slots): return False
    item = slots[slot - 1]
    return isinstance(item, dict) and str(item.get('cid', '')) not in sessions

def _game_available_areas(game: dict[str, object]) -> list[int]:
    """Areas which the 20-area Flash map may currently select."""
    occupied = {int(area) for area in dict(game.get('bases', {})).values()}
    occupied.update(int(area) for area in dict(game.get('claims', {})))
    return [area for area in range(1, MAP_AREA_COUNT + 1) if area not in occupied]

def _game_available_mask(game: dict[str, object]) -> int:
    mask = 0
    for area in _game_available_areas(game):
        mask |= 1 << (area - 1)
    return mask

def _encode_base(area: object, base_state: int = 0) -> int:
    """Encode the client B byte: low 5 bits area, high 2 bits damage state."""
    value = max(0, min(31, int(area)))
    return value | ((max(0, min(3, int(base_state))) & 3) << 6)

def _game_points(game: dict[str, object]) -> list[int]:
    bases = dict(game.get('bases', {})); claims = dict(game.get('claims', {}))
    return [(1000 if str(slot) in bases else 0) +
            200 * sum(1 for owner in claims.values() if int(owner) == slot)
            for slot in range(1, 4)]


def _friendly_complete_bot_selection(room: dict[str, object]) -> None:
    """Complete an AI base/conquest choice after its animation has started.

    This is called by the existing listener poll (not a background worker), and
    changes the generation only when a real selection result becomes available.
    """
    game = room.get('game')
    if not isinstance(game, dict) or game.get('stage') not in ('select', 'free_select'):
        return
    slot = int(game.get('next_player', 0))
    if not _friendly_is_bot(room, slot): return
    due = game.get('bot_select_due')
    if due is None:
        game['bot_select_due'] = time.monotonic() + 2.5
        return
    if time.monotonic() < float(due): return
    available = _game_available_areas(game)
    if not available: return
    area = secrets.choice(available)
    if game.get('stage') == 'select':
        bases = game.setdefault('bases', {})
        bases[str(slot)] = area
        game.update({'last_player': slot, 'next_player': slot,
                     'stage': 'select_result', 'ready': set()})
    else:
        claims = game.setdefault('claims', {})
        claims[str(area)] = slot
        game.update({'free_area': area, 'winner': slot, 'last_player': slot,
                     'stage': 'free_result', 'ready': set()})
    game.pop('bot_select_due', None)
    _friendly_mark(room)


def _friendly_advance_ready(room: dict[str, object], cid: str) -> None:
    game = room.get('game');
    if not isinstance(game, dict): return
    ready = game.setdefault('ready', set())
    if not isinstance(ready, set): ready = game['ready'] = set()
    ready.add(cid)
    humans = set(_friendly_humans(room))
    if not humans.issubset(ready): return
    ready.clear(); stage = str(game.get('stage', 'prepare'))
    bases = game.setdefault('bases', {})
    human_slots = [_friendly_slot(room, member) for member in _friendly_humans(room)]
    if stage == 'prepare':
        game['stage'] = 'select'; game['next_player'] = human_slots[0]
    elif stage == 'select_result':
        # The Flash client must see a distinct turn and selection-result phase
        # for each player, including AI slots, or its opponent drum roll hangs.
        remaining = [slot for slot in range(1, 4) if str(slot) not in bases]
        if remaining:
            next_slot = remaining[0]
            game['stage'] = 'select'
            game['next_player'] = next_slot
            game['bot_select_due'] = (time.monotonic() + 2.5
                                      if _friendly_is_bot(room, next_slot) else None)
        else:
            game['stage'] = 'free0'; game['next_player'] = 1
            game.pop('bot_select_due', None)
    elif stage == 'free0':
        game['stage'] = 'free_question'; game['answers'] = {}
        _select_question(game)
    elif stage == 'tip_result':
        game['stage'] = 'free_select'; game['next_player'] = int(game.get('winner', 1))
        if _friendly_is_bot(room, int(game['next_player'])):
            game['bot_select_due'] = time.monotonic() + 2.5
    elif stage == 'free_result':
        game['round'] = int(game.get('round', 0)) + 1
        no_neutral_areas = not _game_available_areas(game)
        game['stage'] = ('gameover' if no_neutral_areas or
                         int(game['round']) >= MATCH_ROUNDS else 'free0')
    elif stage == 'gameover': game['stage'] = 'aftergame'
    if game.get('stage') != stage:
        _friendly_mark(room)

def _load_accounts() -> dict[str, object]:
    try:
        with open(ACCOUNTS_FILE, encoding='utf-8') as fh:
            value = json.load(fh)
            return value if isinstance(value, dict) else {'next_id': 1, 'accounts': {}}
    except FileNotFoundError:
        return {'next_id': 1, 'accounts': {}}

def _save_accounts(value: dict[str, object]) -> None:
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    temp = ACCOUNTS_FILE + '.tmp'
    with open(temp, 'w', encoding='utf-8') as fh:
        json.dump(value, fh, indent=2, sort_keys=True)
        fh.flush(); os.fsync(fh.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, ACCOUNTS_FILE)

def _password_record(password: str) -> tuple[str, str]:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 240_000)
    return salt.hex(), digest.hex()

def _password_ok(account: dict[str, object], password: str) -> bool:
    try:
        salt = bytes.fromhex(str(account['salt']))
        expected = bytes.fromhex(str(account['password_hash']))
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 240_000)
        return hmac.compare_digest(actual, expected)
    except (KeyError, ValueError):
        return False

def _new_account(db: dict[str, object], username: str, password: str,
                 *, guest: bool = False) -> dict[str, object]:
    user_id = int(db.get('next_id', 1))
    db['next_id'] = user_id + 1
    salt, digest = _password_record(password)
    now = int(time.time())
    settings = _settings()
    account = {
        'id': user_id, 'username': username, 'salt': salt,
        'password_hash': digest, 'guid': secrets.token_hex(16),
        'guest': guest, 'created_at': now, 'last_login': now,
        'gold': max(0, int(settings.get('start_gold', 1000))), 'level': 1, 'xp': 0,
        'friend_code': _new_friend_code(db),
        'friends': [], 'role': 'puppy' if username.casefold() == 'ari' else 'member',
        # Keep the village usable, but start actual progression at entry level.
        # Hints and upgrades are earned from matches/season tiers rather than
        # being preloaded as a debug bundle.
        'buildings_unlocked': True, 'castle_level': 1,
        'energy': 15, 'lives': 3, 'powerups': 0,
        'season_id': str(settings.get('season_id', 'revival-1')),
        'season_xp': 0, 'season_tier': 0, 'season_claimed': [],
    }
    accounts = db.setdefault('accounts', {})
    assert isinstance(accounts, dict)
    accounts[username.casefold()] = account
    return account

def _new_friend_code(db: dict[str, object]) -> str:
    """Generate a short code that is comfortable to type on a phone."""
    alphabet = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'
    used = {str(a.get('friend_code', '')).upper()
            for a in dict(db.get('accounts', {})).values() if isinstance(a, dict)}
    while True:
        code = ''.join(secrets.choice(alphabet) for _ in range(8))
        if code not in used: return code

def _ensure_friend_fields(db: dict[str, object]) -> bool:
    changed = False
    for account in dict(db.get('accounts', {})).values():
        if not isinstance(account, dict): continue
        if not account.get('friend_code'):
            account['friend_code'] = _new_friend_code(db); changed = True
        if not isinstance(account.get('friends'), list):
            account['friends'] = []; changed = True
        defaults = {
            'role': 'puppy' if str(account.get('username', '')).casefold() == 'ari' else 'member',
            'xp': max(0, (int(account.get('level', 1)) - 1) * 100),
            'buildings_unlocked': True, 'castle_level': 1,
            'energy': 15, 'lives': 3, 'powerups': 0,
            'season_id': str(_settings().get('season_id', 'revival-1')),
            'season_xp': 0, 'season_tier': 0, 'season_claimed': [],
        }
        for key, value in defaults.items():
            if key not in account: account[key] = value; changed = True
    return changed

def _role(account: dict[str, object]) -> str:
    value = str(account.get('role', 'member')).lower()
    return value if value in ('puppy', 'admin', 'member') else 'member'

def _display_name(account: dict[str, object]) -> str:
    labels = {'puppy': 'Puppy', 'admin': 'Admin', 'member': 'Member'}
    return f"[{labels[_role(account)]}] {account.get('username', 'Player')}"

def _account_in_db(db: dict[str, object], user_id: object) -> dict[str, object] | None:
    for account in dict(db.get('accounts', {})).values():
        if isinstance(account, dict) and str(account.get('id')) == str(user_id):
            return account
    return None

def _account_by_friend_code(db: dict[str, object], code: object) -> dict[str, object] | None:
    wanted = re.sub(r'[^A-Z0-9]', '', str(code).upper())
    for account in dict(db.get('accounts', {})).values():
        if isinstance(account, dict) and str(account.get('friend_code', '')).upper() == wanted:
            return account
    # Keep the old numeric player id useful in Server Manager and test builds.
    return _account_in_db(db, wanted) if wanted.isdigit() else None

def _friend_items(db: dict[str, object], account: dict[str, object]) -> list[dict[str, object]]:
    items = []
    for user_id in account.get('friends', []):
        friend = _account_in_db(db, user_id)
        if not friend: continue
        items.append({'id': str(friend['id']), 'name': _display_name(friend),
                      'int_avatar': 'revival', 'ext_avatar': '', 'flag': 1,
                      'actleague': 1, 'xplevel': int(friend.get('level', 1))})
    return items

def _login_payload(account: dict[str, object], host: str, port: int) -> dict[str, object]:
    now = int(time.time())
    sign = secrets.token_hex(16)
    login_tickets[sign] = {'user_id': str(account['id']),
                           'username': str(account['username']),
                           'display_name': _display_name(account),
                           'guid': str(account['guid']), 'expires': now + 300}
    stoc = secrets.token_urlsafe(24)
    web_tokens[stoc] = {'user_id': str(account['id']), 'expires': now + 86400 * 30}
    return {'error': 0, 'errormsg': '', 'data': {
        'userid': str(account['id']), 'username': str(account['username']),
        'userlastname': '', 'useremail': '', 'guid': str(account['guid']),
        'sign': sign, 'time': str(now), 'stoc': stoc,
        'currency': 'CZK', 'extid': '',
        'server': {'serveraddress': host, 'httpport': port,
                   'xsocketaddress': host, 'xsocketport': 0},
        'sysconf': {}}}

def rotl(v: int, n: int) -> int:
    return ((v << n) | (v >> (32 - n))) & MASK

def block(key: bytes, nonce: bytes, counter: int = 0) -> bytes:
    if len(key) != 32 or len(nonce) != 8: raise ValueError('key/nonce')
    s = list(struct.unpack('<4I', b'expand 32-byte k')) + list(struct.unpack('<8I', key)) + [counter & MASK, counter >> 32] + list(struct.unpack('<2I', nonce))
    x = s[:]
    for _ in range(4):
        for a,b,c,d in ((0,4,8,12),(1,5,9,13),(2,6,10,14),(3,7,11,15),(0,5,10,15),(1,6,11,12),(2,7,8,13),(3,4,9,14)):
            x[a]=(x[a]+x[b])&MASK; x[d]=rotl(x[d]^x[a],16); x[c]=(x[c]+x[d])&MASK; x[b]=rotl(x[b]^x[c],12)
            x[a]=(x[a]+x[b])&MASK; x[d]=rotl(x[d]^x[a],8); x[c]=(x[c]+x[d])&MASK; x[b]=rotl(x[b]^x[c],7)
    return struct.pack('<16I', *((x[i]+s[i]) & MASK for i in range(16)))

def crypt(data: bytes) -> bytes:
    if len(data) < 8: raise ValueError('short frame')
    nonce, plain = data[:8], data[8:]
    out = bytearray(data)
    for pos in range(0, len(plain), 64):
        stream = block(KEY, nonce, pos // 64)
        for j, value in enumerate(plain[pos:pos+64]): out[8+pos+j] = value ^ stream[j]
    return bytes(out)

def unpack_frame(raw: bytes) -> str:
    if len(raw) < 4: raise ValueError('missing header')
    # AIR ByteArray defaults to little-endian here: 00 a3 01 00 represents
    # (419 << 8), i.e. the complete 419-byte frame.
    length_flags, = struct.unpack('<I', raw[:4]); length = length_flags >> 8
    if length != len(raw): raise ValueError(f'bad length {length}/{len(raw)}')
    return crypt(raw[4:])[8:].decode('utf-8')

def pack_frame(xml: str) -> bytes:
    nonce = secrets.token_bytes(8)
    body = crypt(nonce + xml.encode('utf-8'))
    return struct.pack('<I', (len(body) + 4) << 8) + body

def attrs(**kw: object) -> str:
    def esc(value: object) -> str:
        return str(value).replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;')
    return ' '.join(f'{k}="{esc(v)}"' for k,v in kw.items())

def _account_by_id(user_id: str) -> dict[str, object] | None:
    with account_lock:
        db = _load_accounts()
        return next((a for a in dict(db.get('accounts', {})).values()
                     if isinstance(a, dict) and str(a.get('id')) == str(user_id)), None)

def _tax_state(account: dict[str, object], now: int | None = None) -> tuple[int, int]:
    """Return the castle reward and seconds until it may be collected again."""
    current = int(time.time()) if now is None else int(now)
    reward = max(0, int(account.get('castle_tax_reward', 1000)))
    last = max(0, int(account.get('last_tax_collect', 0)))
    remaining = max(0, 86400 - (current - last)) if last else 0
    return reward, remaining

def _collect_tax(user_id: str, now: int | None = None) -> tuple[int, int]:
    """Collect the daily castle income, persist it, and return (reward, gold)."""
    with account_lock:
        db = _load_accounts(); account = _account_in_db(db, user_id)
        if account is None:
            return 0, 0
        reward, remaining = _tax_state(account, now)
        if remaining > 0:
            return 0, int(account.get('gold', 0))
        account['gold'] = int(account.get('gold', 0)) + reward
        account['last_tax_collect'] = int(time.time()) if now is None else int(now)
        _save_accounts(db)
        return reward, int(account['gold'])

def player_data(session: dict[str, str]) -> str:
    # Fields consumed by syscode.Sys.tagproc_MYDATA; defaults keep boot stable.
    account = _account_by_id(session.get('user_id', '')) or {}
    # The revival has no retired progression economy, so expose the complete
    # village/building set while retaining the stored level in Server Manager.
    unlocked = bool(account.get('buildings_unlocked', True))
    level = max(1, int(account.get('level', 1)))
    castle = int(account.get('castle_level', 1)); energy = int(account.get('energy', 15))
    lives = int(account.get('lives', 3)); powerups = int(account.get('powerups', 0))
    gold = int(account.get('gold', 1000))
    tax_reward, tax_remaining = _tax_state(account)
    unlock_flags = ','.join(['1' if unlocked else '0'] * 21)
    xp = max(0, int(account.get('xp', 0)))
    return '<MYDATA ' + attrs(ID=session.get('user_id', '1'), NAME=session['name'], COUNTRY='CZ', SEX=0, BIRTHDAY='', GAMECOUNT=int(account.get('game_count', 0)), GAMECOUNTSR=0, FLAGS=0, WINDOWFLAGS=0, CASTLELEVEL=f'{castle},0', XPPACK=f'{xp},{level},{max(0, (level - 1) * 100)},{level * 100}', ACTLEAGUE=1, VEP='0,0,0', FIRSTLOGIN=0, LASTGAME=0, PLACES='0,0,0', VICTORY='0,0', USQACCEPTED=1, ULS=unlock_flags, ACTION='', TUTORIALMISSION='', GOLD=gold, ENERGYPACK=f'{energy},{energy},0,300,1,0', MTCUPS='', SOLDIER=1, CUSTOMAVATAR='', USECUSTOMAVATAR=0, EXTAVATAR='', MYCATEGORY=0, TAXDATA=f'{tax_reward},{tax_remaining},{tax_reward},0,0,0,0', LIVES=f'{lives},0', FH=','.join([str(powerups)] * 12), HFS='|'.join([f'1,{powerups},1,0'] * 9), LT='0', FH_LIFE_EXPIRE=0, MISSIONS='0,0,0', CWINS='0,0,0,0,0,0,0', SOLDIERS='1', WEEKLY='0,0,0,0', LASTPLACES='', SMSR='', SNDVOL=4095) + ' />'

def _friendly_game_tags(room: dict[str, object], cid: str) -> list[str]:
    game = room.get('game')
    if not isinstance(game, dict): return []
    stage = str(game.get('stage', 'prepare'))
    states = {'prepare': '11,1,0', 'select': '1,1,1', 'select_result': '1,1,3',
              'free0': '3,1,0', 'free_question': '3,1,1', 'tip_result': '3,1,3',
              'free_select': '3,1,4', 'free_result': '3,1,6',
              'gameover': '15,0,0', 'aftergame': '16,0,0'}
    bases = game.setdefault('bases', {}); claims = game.setdefault('claims', {})
    next_player = int(game.get('next_player', 1)); selections = [0, 0, 0]
    if stage == 'select_result':
        slot = int(game.get('last_player', next_player)); selections[slot - 1] = int(bases.get(str(slot), 0))
    elif stage == 'free_result':
        selections[int(game.get('winner', 1)) - 1] = int(game.get('free_area', 0))
    # Semu.AddTag_STATE is the protocol oracle: SEL is three raw area bytes;
    # B stores area+(baseDamageState<<6). A new intact base has state zero.
    sel = ''.join(f'{n:02x}' for n in selections)
    base_bytes = ''.join(f'{_encode_base(bases.get(str(slot), 0)):02x}'
                         for slot in range(1, 4))
    area_values = ['00'] * MAP_AREA_COUNT
    for slot in range(1, 4):
        area = int(bases.get(str(slot), 0))
        if area: area_values[area - 1] = f'{0x10 + slot:02x}'
    for area, owner in dict(claims).items():
        if 1 <= int(area) <= MAP_AREA_COUNT:
            area_values[int(area) - 1] = f'{0x40 + int(owner):02x}'
    points = _game_points(game)
    last_player = int(game.get('last_player', next_player))
    current_player = last_player if stage == 'select_result' else next_player
    tags = [
        '<STATE ' + attrs(SCR='MAP_WD', ST=states.get(stage, '3,1,0'), CP=f'{current_player},{next_player}',
                          HC='123', CHS='0,0,0', PTS=','.join(map(str, points)), SEL=sel,
                          B=base_bytes, A=''.join(area_values),
                          AA=f'{_game_available_mask(game):06x}', UH='0', RT='F') + ' />'
    ]
    names: list[str] = []; player_data_fields: list[str] = []
    for slot in room.get('slots', []):
        assert isinstance(slot, dict); member = str(slot.get('cid', ''))
        if member in sessions:
            names.append(sessions[member]['name'])
            player_data_fields.append(f"{sessions[member]['user_id']},0,1,1,0,CZ,1,,1,1")
        else:
            names.append('Robot'); player_data_fields.append('-1,0,1,1,0,CZ,1,,1,1')
    iam = _friendly_slot(room, cid); others = [n for n in range(1, 4) if n != iam]
    tags.append('<PLAYERS ' + attrs(P1=names[0], P2=names[1], P3=names[2],
        PD1=player_data_fields[0], PD2=player_data_fields[1], PD3=player_data_fields[2],
        YOU=','.join(map(str, [iam] + others)), GAMEID=room['id'], ROOM=room['id'], RULES='0,0') + ' />')
    if stage == 'select' and next_player == iam:
        tags.append('<CMD ' + attrs(CMD='SELECT',
                                     AVAILABLE=f'{_game_available_mask(game):06x}', TO='30') + ' />')
    elif stage == 'free_question':
        question = _current_question(game)
        tags += ['<TIPQUESTION ' + attrs(QUESTION=question['question'], ALLOWMARK=1,
                                         THEME=int(question.get('category', 4)),
                                         HELP='{AVERAGE:-3,RANGE:-3}') + ' />',
                 '<CMD CMD="TIP" PLAYERS="3" TO="30" />']
    elif stage == 'tip_result':
        answers = game.get('answers', {}) if isinstance(game.get('answers'), dict) else {}
        vals = [str(answers.get(str(slot), 1348 + slot * 7)) for slot in range(1, 4)]
        question = _current_question(game)
        winner = int(game.get('winner', 1)); second = next(slot for slot in range(1, 4) if slot != winner)
        tags += ['<TIPINFO ' + attrs(TIMEORDER='123', T1='1', V1=vals[0], A1='100', T2='2', V2=vals[1], A2='99', T3='3', V3=vals[2], A3='90') + ' />',
                 '<TIPRESULT ' + attrs(WINNER=winner, SECOND=second,
                                        GOOD=int(question['answer'])) + ' />']
    elif stage == 'free_select' and next_player == iam:
        tags.append('<CMD ' + attrs(CMD='SELECT',
                                     AVAILABLE=f'{_game_available_mask(game):06x}',
                                     MAPHELPS='{AIRBORNE:-3,SUBJECT:-3,FORTRESS:-3}',
                                     TO='30') + ' />')
    elif stage == 'gameover':
        order = sorted(range(1, 4), key=lambda slot: points[slot - 1], reverse=True)
        place = order.index(iam) + 1 if iam in order else 3
        earned_xp = int(sessions.get(cid, {}).get('last_match_xp', 0))
        total_gold = int(sessions.get(cid, {}).get('last_match_gold_total', 0))
        tags += ['<GAMEOVER ' + attrs(PLACINGS=''.join(map(str, order)), ROOMID=room['id'], XP=f'0,{earned_xp},1,0', XPPL=f'{place},{earned_xp}', XPPP='38,0', XPOPP='1,0', XPCW='1,0', XPCW2='1,0', ANSWERS='0,0', TIPS='3,3', VEPTIPS='3,300', GOLDS=total_gold, RL='0,100,0') + ' />', '<DIVISION UP="0" DOWN="0" />']
    return tags

def reply(channel: str, cid: str, mn: str, inner: str = '') -> str:
    return f'<{channel} CID="{cid}" MN="{mn}" R="0" />\r\n{inner}\r\n'

def mark_dirty(session: dict[str, str]) -> None:
    session['generation'] = str(int(session.get('generation', '0')) + 1)

def record_game_complete(session: dict[str, str], *, won: bool = False) -> None:
    if session.get('game_recorded') == '1': return
    with account_lock:
        db = _load_accounts()
        for account in dict(db.get('accounts', {})).values():
            if isinstance(account, dict) and str(account.get('id')) == session.get('user_id'):
                account['game_count'] = int(account.get('game_count', 0)) + 1
                account['last_game'] = int(time.time())
                earned_gold = 250 if won else 100
                earned_xp = 100 if won else 40
                account['gold'] = int(account.get('gold', 0)) + earned_gold
                account['xp'] = int(account.get('xp', 0)) + earned_xp
                account['level'] = max(int(account.get('level', 1)),
                                       min(200, int(account['xp']) // 100 + 1))
                session.update({'last_match_gold': str(earned_gold),
                                'last_match_gold_total': str(account['gold']),
                                'last_match_xp': str(earned_xp)})
                _save_accounts(db); break
    session['game_recorded'] = '1'

class Handler(BaseHTTPRequestHandler):
    server_version = 'DobyvatelRevival/0.1'
    def log_message(self, fmt, *args):
        query = parse_qs(urlsplit(self.path).query)
        if urlsplit(self.path).path == '/game' and query.get('CH', [''])[0] in ('L', 'H'):
            return
        print(f'{self.client_address[0]} - {fmt % args}', flush=True)

    def send_json(self, obj: object) -> None:
        data = json.dumps(obj, separators=(',', ':')).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def public_endpoint(self) -> tuple[str, int]:
        host = os.environ.get('DOBYVATEL_PUBLIC_HOST', '').strip()
        if not host:
            host = self.headers.get('Host', '127.0.0.1').split(':', 1)[0]
        port = int(os.environ.get('DOBYVATEL_PUBLIC_PORT',
                                  str(self.server.server_address[1])))
        return host, port

    def legacy_json(self, path: str) -> bool:
        """Serve the small PHP/JSON APIs loaded while the village boots."""
        if path == '/client_castle.php':
            badge_names = ('CW1', 'CW2', 'XPT', 'XPM', 'RLP', 'TWD', 'USQ', 'EXT')
            all_badges: dict[str, object] = {
                'NEWLEVELS': {name: 0 for name in badge_names}}
            all_badges.update({name: [0] * 7 for name in badge_names})
            self.send_json({'error': 0, 'errormsg': '', 'data': {
                'castlebadges': [None] * 6, 'allbadges': all_badges}}); return True
        if path == '/client_messages.php':
            self.send_json({'error': 0, 'errormsg': '', 'data': []}); return True
        if path == '/client_clans.php':
            self.send_json({'error': 0, 'errormsg': '', 'data': {
                'invites': 0, 'clanid': 0}}); return True
        if path == '/client_friends.php':
            self.send_json({'error': 401, 'errormsg': 'Sign in again.', 'data': {
                'allitems': [], 'friendcode': ''}}); return True
        return False

    def client_friends(self, query: dict[str, list[str]], raw: bytes) -> None:
        """Legacy friend list, extended with Revival friend-code pairing."""
        token = query.get('stoc', [''])[0]
        auth = web_tokens.get(token)
        if not auth or int(auth.get('expires', 0)) < int(time.time()):
            self.send_json({'error': 602, 'errormsg': 'Session expired.',
                            'data': {'allitems': [], 'friendcode': ''}}); return
        try:
            req = json.loads(raw or b'{}')
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json({'error': 2, 'errormsg': 'Invalid request.',
                            'data': {'allitems': [], 'friendcode': ''}}); return
        command = str(req.get('cmd', 'list'))
        partner = str(req.get('partnerid', '')).strip()
        with account_lock:
            db = _load_accounts(); changed = _ensure_friend_fields(db)
            account = _account_in_db(db, auth['user_id'])
            if account is None:
                self.send_json({'error': 602, 'errormsg': 'Account missing.',
                                'data': {'allitems': [], 'friendcode': ''}}); return
            if command == 'add':
                other = _account_by_friend_code(db, partner)
                if other is None:
                    self.send_json({'error': 404, 'errormsg': 'Friend Code not found.',
                                    'data': {'allitems': _friend_items(db, account),
                                             'friendcode': account['friend_code']}}); return
                if str(other['id']) == str(account['id']):
                    self.send_json({'error': 409, 'errormsg': 'That is your Friend Code.',
                                    'data': {'allitems': _friend_items(db, account),
                                             'friendcode': account['friend_code']}}); return
                for left, right in ((account, other), (other, account)):
                    friends = left.setdefault('friends', [])
                    if str(right['id']) not in {str(value) for value in friends}:
                        friends.append(str(right['id'])); changed = True
            elif command in ('cancel_friendship', 'deny', 'block'):
                other = _account_by_friend_code(db, partner)
                if other is None: other = _account_in_db(db, partner)
                if other is not None:
                    for left, right in ((account, other), (other, account)):
                        friends = left.setdefault('friends', [])
                        filtered = [value for value in friends
                                    if str(value) != str(right['id'])]
                        if len(filtered) != len(friends):
                            left['friends'] = filtered; changed = True
            elif command not in ('list', 'cancel_block'):
                self.send_json({'error': 1, 'errormsg': 'Unsupported friend command.',
                                'data': {'allitems': _friend_items(db, account),
                                         'friendcode': account['friend_code']}}); return
            if changed: _save_accounts(db)
            data = {'allitems': _friend_items(db, account),
                    'friendcode': str(account['friend_code'])}
        self.send_json({'error': 0, 'errormsg': '', 'data': data})

    def client_userdata(self, query: dict[str, list[str]], raw: bytes) -> None:
        token = query.get('stoc', [''])[0]; auth = web_tokens.get(token)
        if not auth:
            self.send_json({'error': 602, 'errormsg': 'Session expired.', 'data': {}}); return
        try: req = json.loads(raw or b'{}')
        except Exception: req = {}
        with account_lock:
            db = _load_accounts()
            account = _account_in_db(db, req.get('userid', auth['user_id']))
        if account is None:
            self.send_json({'error': 404, 'errormsg': 'Player not found.', 'data': {}}); return
        level = int(account.get('level', 1)); now = int(time.time())
        season = _season_status(account)
        data = {'id': str(account['id']), 'name': _display_name(account),
                'xplevel': level, 'xppoints': 0, 'xpmin': 0, 'xpmax': 100,
                'actleague': 1, 'jep': 0, 'jeplevel': 1, 'castlelevel': 7,
                'firstlogin': str(account.get('created_at', now)),
                'lastgame': str(account.get('last_game', 0)),
                'gamecount': int(account.get('game_count', 0)), 'gamecountsr': 0,
                'map50pcnt': 0, 'map100pcnt': 0, 'place1cnt': 0, 'place2cnt': 0,
                'place3cnt': 0, 'rl_gamecount': 0, 'rl_towers': 0, 'soldier': 1,
                'clan_id': int(account.get('clan_id', 0)),
                'clan_name': str(account.get('clan_name', '')), 'clan_level': 1,
                'clan_invited': 0, 'clan_adminright': 2 if _role(account) == 'puppy' else
                (1 if _role(account) == 'admin' else int(account.get('clan_adminright', 0))),
                'season': season, 'season_xp': season['xp'],
                'season_tier': season['tier'], 'season_name': season['name']}
        self.send_json({'error': 0, 'errormsg': '', 'data': data})

    def client_season(self, query: dict[str, list[str]]) -> None:
        token = query.get('stoc', [''])[0]; auth = web_tokens.get(token)
        if not auth or int(auth.get('expires', 0)) < int(time.time()):
            self.send_json({'error': 602, 'errormsg': 'Session expired.', 'data': {}}); return
        with account_lock:
            db = _load_accounts(); account = _account_in_db(db, auth['user_id'])
            if account is None:
                self.send_json({'error': 404, 'errormsg': 'Player not found.', 'data': {}}); return
            before = dict(account); status = _season_status(account)
            if account != before: _save_accounts(db)
        self.send_json({'error': 0, 'errormsg': '', 'data': status})

    def client_clans(self, query: dict[str, list[str]], raw: bytes) -> None:
        """Small persistent clan core; unsupported views receive correctly shaped empties."""
        token = query.get('stoc', [''])[0]; auth = web_tokens.get(token)
        try: req = json.loads(raw or b'{}')
        except Exception: req = {}
        command = str(req.get('cmd', 'menubutton'))
        if not auth:
            self.send_json({'error': 602, 'errormsg': 'Session expired.', 'data': {}}); return
        with account_lock:
            db = _load_accounts(); account = _account_in_db(db, auth['user_id'])
            if account is None:
                self.send_json({'error': 404, 'errormsg': 'Player not found.', 'data': {}}); return
            clans = db.setdefault('clans', {})
            clan = clans.get(str(account.get('clan_id', ''))) if isinstance(clans, dict) else None
            if command == 'found':
                name = str(req.get('name', '')).strip()
                if not 4 <= len(name) <= 24:
                    self.send_json({'error': 11, 'errormsg': 'Clan name must be 4-24 characters.', 'data': {}}); return
                if any(str(c.get('name', '')).casefold() == name.casefold()
                       for c in clans.values() if isinstance(c, dict)):
                    self.send_json({'error': 14, 'errormsg': 'Clan name exists.', 'data': {}}); return
                clan_id = str(max([int(x) for x in clans.keys() if str(x).isdigit()] + [0]) + 1)
                clan = {'id': clan_id, 'name': name, 'intro': str(req.get('intro', '')),
                        'owner': str(account['id']), 'members': [str(account['id'])],
                        'founded': time.strftime('%Y-%m-%d'), 'shield': 0, 'xplevel': 1}
                clans[clan_id] = clan
                account.update({'clan_id': int(clan_id), 'clan_name': name, 'clan_adminright': 2})
                _save_accounts(db)
            def summary(value):
                if not value: return {'id': 0, 'name': '', 'xplevel': 0, 'members': 0,
                                      'invites': 0, 'signups': 0, 'blocked': 0, 'namechange': 0,
                                      'week_close': 0}
                return {'id': value['id'], 'name': value['name'],
                        'xplevel': value.get('xplevel', 1), 'members': len(value.get('members', [])),
                        'invites': 0, 'signups': 0, 'blocked': 0, 'namechange': 0,
                        'week_close': 0}
            if command == 'menubutton': data = summary(clan)
            elif command == 'rosterlist': data = [summary(c) for c in clans.values()]
            elif command in ('ranking', 'search', 'alliance_list'):
                data = {'own': summary(clan), 'clans': [summary(c) for c in clans.values()]}
            elif command == 'get_alliancesignups': data = []
            elif command == 'history': data = {'history': []}
            elif command == 'alliance':
                wanted = clans.get(str(req.get('allianceid', account.get('clan_id', ''))), clan)
                members = []
                if wanted:
                    for uid in wanted.get('members', []):
                        member = _account_in_db(db, uid)
                        if member: members.append({'userid': str(uid), 'username': member['username'],
                                                   'adminright': 2 if str(uid) == str(wanted.get('owner')) else 0,
                                                   'xppoints': 0, 'xplevel': int(member.get('level', 1))})
                data = {'alliance': dict(summary(wanted), intro=(wanted or {}).get('intro', ''),
                                         admission=2, adminright=int(account.get('clan_adminright', 0)),
                                         cond=0, cond_minleague=7, cond_minxplevel=0,
                                         lastlevelxp=0, nextlevelxp=100, xppoints=0,
                                         xppoints_weekly=0, xppoints_weekly_prev=0,
                                         founded=(wanted or {}).get('founded', ''), shield=(wanted or {}).get('shield', 0)),
                        'members': members, 'friends': [], 'history': [], 'pages': 0}
            else: data = {}
        self.send_json({'error': 0, 'errormsg': '', 'data': data})

    def do_GET(self):
        split = urlsplit(self.path)
        path = split.path
        query = parse_qs(split.query)
        print(f'GET {self.path}', flush=True)
        if path == '/client_season.php':
            self.client_season(query); return
        if self.legacy_json(path): return
        if path == '/health':
            data = b'{"ok":true}'
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data); return
        payloads = {
            '/crossdomain.xml': ('application/xml', '<cross-domain-policy><allow-access-from domain="*" to-ports="*" /></cross-domain-policy>'),
            '/dat/help.json': ('application/json', '{}'),
            '/client_countries.php': ('application/json', '{"error":0,"data":[{"id":"CZ","name":"Česko","description":"","flag":""}]}'),
        }
        if path not in payloads: self.send_error(404); return
        content_type, payload = payloads[path]; data = payload.encode('utf-8')
        self.send_response(200); self.send_header('Content-Type', content_type); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        global next_friendly_room
        split = urlsplit(self.path)
        query = parse_qs(split.query)
        quiet_poll = split.path == '/game' and query.get('CH', [''])[0] in ('L', 'H')
        if not quiet_poll:
            print(f'POST {self.path}', flush=True)
        n = int(self.headers.get('Content-Length', '0'))
        if n > 262144: self.send_error(413); return
        raw = self.rfile.read(n)
        path = urlsplit(self.path).path
        if path == '/client_friends.php':
            print(f'LEGACY_RX {path} {raw.decode("utf-8", "replace")!r}', flush=True)
            self.client_friends(query, raw); return
        if path == '/client_userdata.php':
            self.client_userdata(query, raw); return
        if path == '/client_season.php':
            self.client_season(query); return
        if path == '/client_clans.php':
            print(f'LEGACY_RX {path} {raw.decode("utf-8", "replace")!r}', flush=True)
            self.client_clans(query, raw); return
        if path in ('/client_castle.php', '/client_messages.php',
                    ):
            print(f'LEGACY_RX {path} {raw.decode("utf-8", "replace")!r}', flush=True)
            self.legacy_json(path); return
        if path == '/client_extdata.php':
            self.send_json({'error': 0, 'errormsg': '', 'data': []}); return
        if path == '/client_errorlog.php':
            decoded = raw.decode('utf-8', 'replace')
            try:
                report = json.loads(decoded)
                state = report.get('state') or {}
                print('CLIENT_ERROR '
                      f'message={report.get("message", "")} '
                      f'screen={state.get("screen", "")} '
                      f'window={state.get("currentwindow", "")}\n'
                      f'{report.get("stacktrace", "")}', flush=True)
            except Exception:
                print(f'CLIENT_ERROR {decoded!r}', flush=True)
            os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
            with open(os.path.join(DATA_DIR, 'client_errors.jsonl'), 'a', encoding='utf-8') as fh:
                fh.write(decoded.replace('\n', '\\n') + '\n')
            data = b'{"error":0}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers(); self.wfile.write(data)
            return
        if path == '/mobil.php':
            try:
                req = json.loads(raw or b'{}')
                logged = dict(req)
                for secret_key in ('password', 'pass', 'guid', 'sign'):
                    if secret_key in logged: logged[secret_key] = '<redacted>'
                print(f'RX {json.dumps(logged, separators=(",", ":"))!r}', flush=True)
                command = req.get('cmd', '')
                if command == 'ping':
                    obj = {'error': 0, 'errormsg': 'pong', 'data': {}}
                elif command == 'register_guid':
                    with account_lock:
                        db = _load_accounts()
                        _ensure_friend_fields(db)
                        password = secrets.token_hex(12)
                        account = _new_account(db, 'Guest-' + secrets.token_hex(3),
                                               password, guest=True)
                        _save_accounts(db)
                    obj = {'error': 0, 'errormsg': '', 'data': {
                        'guid': account['guid'], 'pass': password}}
                elif command == 'login_guid':
                    guid = str(req.get('guid', ''))
                    password = str(req.get('password', ''))
                    account = None
                    with account_lock:
                        db = _load_accounts()
                        migrated = _ensure_friend_fields(db)
                        for candidate in dict(db.get('accounts', {})).values():
                            if (isinstance(candidate, dict) and
                                    candidate.get('guid') == guid and
                                    _password_ok(candidate, password)):
                                account = candidate; break
                        if account is not None:
                            account['last_login'] = int(time.time())
                            _save_accounts(db)
                        elif migrated:
                            _save_accounts(db)
                    if account is None:
                        obj = {'error': 2, 'errormsg': 'Invalid guest login.', 'data': {}}
                    else:
                        obj = _login_payload(account, *self.public_endpoint())
                elif command in ('login', 'register'):
                    # The original login panel already has exactly two fields.
                    # First use creates the account; following uses sign in.
                    username = str(req.get('loginname') or req.get('username') or '').strip()
                    password = str(req.get('password') or '')
                    if (not re.fullmatch(r'[A-Za-z0-9 _.-]{3,24}', username) or
                            not (4 <= len(password) <= 128)):
                        obj = {'error': 2, 'errormsg':
                               'Username must be 3-24 characters and password at least 4.',
                               'data': {}}
                    else:
                        with account_lock:
                            db = _load_accounts()
                            _ensure_friend_fields(db)
                            accounts = db.setdefault('accounts', {})
                            account = accounts.get(username.casefold())
                            if account is None:
                                account = _new_account(db, username, password)
                            elif not _password_ok(account, password):
                                account = None
                            if account is not None:
                                account['last_login'] = int(time.time())
                                _save_accounts(db)
                        if account is None:
                            obj = {'error': 2, 'errormsg': 'Wrong username or password.',
                                   'data': {}}
                        else:
                            obj = _login_payload(account, *self.public_endpoint())
                else:
                    obj = {'error': 1, 'errormsg': 'Unsupported command: ' + command,
                           'data': {}}
                data = json.dumps(obj, separators=(',', ':')).encode()
                print(f'TX {data.decode()}', flush=True)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers(); self.wfile.write(data)
            except Exception as exc:
                self.send_error(400, str(exc))
            return
        if path != '/game': self.send_error(404); return
        try:
            if not quiet_poll:
                print(f'RAW {len(raw)} {raw[:64].hex()}', flush=True)
            payload = unpack_frame(raw)
            roots = list(ET.fromstring('<ROOT>'+payload+'</ROOT>'))
            header = roots[0]; channel, cid, mn = header.tag, header.get('CID',''), header.get('MN','0')
            command = roots[1].tag if len(roots) > 1 else ''
            noisy_poll = channel in ('L', 'H') and command in ('LISTEN', '')
            if not noisy_poll:
                print(f'RX {payload!r}', flush=True)
            if channel == 'C' and command == 'LOGIN':
                tag = roots[1]; sign = tag.get('SIGN', '')
                ticket = login_tickets.pop(sign, None)
                valid = bool(ticket and int(ticket.get('expires', 0)) >= int(time.time()) and
                             str(ticket.get('user_id')) == tag.get('UID', '') and
                             str(ticket.get('guid')) == tag.get('GUID', '') and
                             str(ticket.get('username')) == tag.get('NAME', ''))
                if not valid:
                    out = f'<ERROR R="12" MN="{mn}" />\r\n'
                else:
                    cid = secrets.token_hex(16); sessions[cid] = {
                        'name': str(ticket.get('display_name', ticket['username'])),
                        'username': str(ticket['username']), 'user_id': str(ticket['user_id']),
                        'screen': 'VILLAGE', 'listen_count': '0',
                        'generation': '1', 'sent_generation': '0'}
                    out = reply('C', cid, mn)
            elif cid not in sessions:
                out = f'<ERROR R="12" MN="{mn}" />\r\n'
            elif channel == 'L':
                session = sessions[cid]
                friendly = friendly_rooms.get(session.get('friendly_room', ''))
                game_room = _active_game_room(cid)
                if game_room:
                    with friendly_lock:
                        # Comm.Ready uses either a C-channel READY packet or a
                        # LISTEN READY="1" when no long-poll is active.
                        listen_ready = (command == 'LISTEN' and
                                        roots[1].get('READY', '0') == '1')
                        delivered = (session.get('sent_generation', '-1') ==
                                     session.get('generation', '0'))
                        if listen_ready and delivered:
                            _friendly_advance_ready(game_room, cid)
                        _friendly_complete_bot_selection(game_room)
                if (not friendly and session.get('screen') == 'WAIT' and
                        session.get('roomsel', '0') != '0' and
                        time.time() >= float(session.get('start_at', '0'))):
                    game_room = _solo_game_setup(cid)
                first = session.get('listen_count', '0') == '0'
                observed_generation = session.get('generation', '0')
                dirty = observed_generation != session.get('sent_generation', '-1')
                if not first and not dirty:
                    # The original endpoint is a long poll. A short hold avoids
                    # a hot request loop while keeping the client responsive.
                    time.sleep(1.0)
                screen = session.get('screen', 'VILLAGE')
                tags = []
                if first or dirty:
                    if screen == 'MAP_WD':
                        if game_room:
                            tags += _friendly_game_tags(game_room, cid)
                        else:
                            stage = session.get('game_stage', 'prepare')
                            state_by_stage = {
                                'prepare': '11,1,0', 'select': '1,1,1',
                                'select_result': '1,1,3', 'free0': '3,1,0',
                                'free_question': '3,1,1', 'tip_result': '3,1,3',
                                'free_select': '3,1,4', 'free_result': '3,1,6',
                                'gameover': '15,0,0', 'aftergame': '16,0,0',
                            }
                            st = state_by_stage.get(stage, '3,1,0')
                            selected = int(session.get('selected_area', '0'))
                            bot2 = int(session.get('bot2_area', '0'))
                            bot3 = int(session.get('bot3_area', '0'))
                            shown_selection = selected if stage == 'select_result' else (
                                int(session.get('free_area', '0')) if stage == 'free_result' else 0)
                            sel = f'{shown_selection:02x}0000'
                            bases = ''.join(f'{(0xc0 + n) if n else 0xc0:02x}'
                                            for n in (selected, bot2, bot3))
                            area_values = ['00'] * MAP_AREA_COUNT
                            for owner, area in enumerate((selected, bot2, bot3), 1):
                                if 1 <= area <= len(area_values):
                                    area_values[area - 1] = f'{0x10 + owner:02x}'
                            claims = [int(x) for x in session.get('claims', '').split(',') if x]
                            for claim in claims:
                                if 1 <= claim <= len(area_values): area_values[claim - 1] = '41'
                            points = f'{1000 + len(claims) * 200},1000,1000' if selected else '0,0,0'
                            tags += [
                                f'<STATE SCR="MAP_WD" ST="{st}" CP="1,1" HC="123" CHS="0,0,0" PTS="{points}" SEL="{sel}" B="{bases}" A="' + ''.join(area_values) + f'" AA="{(1 << MAP_AREA_COUNT) - 1:06x}" UH="0" RT="C" />',
                                '<PLAYERS ' + attrs(P1=session['name'], P2='Alena', P3='Karel',
                                    PD1='1,0,1,1,0,CZ,1,,1,1', PD2='2,0,1,1,0,CZ,1,,1,1',
                                    PD3='3,0,1,1,0,CZ,1,,1,1', YOU='1,2,3', GAMEID=1,
                                    ROOM=1, RULES='0,0') + ' />',
                            ]
                            if stage == 'select':
                                tags.append(f'<CMD CMD="SELECT" AVAILABLE="{(1 << MAP_AREA_COUNT) - 1:06x}" TO="30" />')
                            elif stage == 'free_question':
                                tags += [
                                    '<TIPQUESTION QUESTION="Ve kterém roce byla založena Univerzita Karlova?" ALLOWMARK="1" THEME="4" HELP="{AVERAGE:-3,RANGE:-3}" />',
                                    '<CMD CMD="TIP" PLAYERS="3" TO="30" />',
                                ]
                            elif stage == 'tip_result':
                                guess = session.get('tip_answer', '1348')
                                tags += [
                                    f'<TIPINFO TIMEORDER="123" T1="1" V1="{guess}" A1="100" T2="2" V2="1350" A2="99" T3="3" V3="1400" A3="90" />',
                                    '<TIPRESULT WINNER="1" SECOND="2" GOOD="1348" />',
                                ]
                            elif stage == 'free_select':
                                tags.append(f'<CMD CMD="SELECT" AVAILABLE="{(1 << MAP_AREA_COUNT) - 1:06x}" MAPHELPS="{{AIRBORNE:-3,SUBJECT:-3,FORTRESS:-3}}" TO="30" />')
                            elif stage == 'gameover':
                                tags += [
                                    '<GAMEOVER PLACINGS="123" ROOMID="1" XP="0,100,1,0" XPPL="1,300" XPPP="38,114" XPOPP="1,3" XPCW="1,0" XPCW2="1,0" ANSWERS="0,0" TIPS="3,3" VEPTIPS="3,300" GOLDS="1000" RL="0,100,0" />',
                                    '<DIVISION UP="0" DOWN="0" />',
                                ]
                    else:
                        tags.append(f'<STATE SCR="{screen}" PO="1" />')
                if first:
                    # GAMEPARAMS must precede MYDATA: tagproc_MYDATA immediately
                    # reads gameparams.forgeupgrades while decoding HFS.
                    tags += [
                        '<FEATURES ENABLED="SEPROOMS" />',
                        '<GAMEPARAMS BADGEBONUSES="3,4,5,6,7,8,9|2,3,4,5,6,7,8|1,2,3,4,5,6,7|1,2,3,4,5,6,7|1,2,3,4,5,6,7|1,2,3,4,5,6,7|4,5,6,7,8,9,10|1,0,0,0,0,0,0" NRG="15,3" HFUG="1,1,24,0|1,1,24,0|1,1,24,0|1,1,24,0|1,1,24,0|1,1,24,0|1,1,24,0|1,1,24,0|1,1,24,0" MP="1,0,10,0,100,0|1,0,10,0,100,0|1,0,10,0,100,0|1,0,10,0,100,0|1,0,10,0,100,0|1,0,10,0,100,0|1,0,10,0,100,0|1,0,2,0,3,0|1,0,2,0,3,0|1,0,2,0,3,0|1,0,2,0,3,0|1,0,2,0,3,0" HP="0,0,0,0,0,0,0,0,0,0,0,0" />',
                        player_data(session),
                    ]
                elif session.pop('season_reward', ''):
                    # Refresh hint/energy/gold inventory immediately after a
                    # completed match instead of requiring another login.
                    tags.append(player_data(session))
                if screen == 'WAIT' and (first or dirty):
                    roomsel = session.get('roomsel', '0')
                    settings = _settings()
                    tags += [
                        '<GAMEROOM ' + attrs(ID=1, TITLE=str(settings.get('server_name', 'Dobyvatel Revival')),
                         TYPE=2, MINLEVEL=1, MAXLEVEL=999, PLAYERS=0,
                         MAXPLAYERS=max(2, min(3, int(settings.get('max_players_per_room', 3)))), CLOSED=0) + ' />',
                        f'<WAITSTATE ROOMSEL="{roomsel}" SEPROOMSEL="0" />',
                        '<SMALLUL UL="" />',
                    ]
                    if friendly:
                        tags.append(_friendly_active_tag(friendly))
                    for public_room in friendly_rooms.values():
                        if not public_room.get('started'):
                            tags.append(_friendly_room_tag(public_room))
                inner = '\r\n'.join(tags)
                session['listen_count'] = str(int(session.get('listen_count', '0')) + 1)
                if dirty:
                    # Mark exactly the generation rendered above. If a C request
                    # changed state concurrently, its newer generation survives.
                    session['sent_generation'] = observed_generation
                out = reply('L', cid, mn, inner)
                if 'TIPQUESTION' in inner or 'CMD="TIP"' in inner or 'SCR="MAP_WD"' in inner:
                    print(
                        f'GAME_UPDATE CID={cid} STAGE={session.get("game_stage")} '
                        f'GEN={observed_generation} DIRTY={dirty} '
                        f'PAYLOAD={inner!r}',
                        flush=True,
                    )
            elif channel == 'C' and command.strip() == 'GETSTOREITEMS':
                out = reply('C', cid, mn,
                            '<STORE><SOLDIERS><P VALUE="1,0,0,0;2,0,0,0;3,0,0,0;4,0,0,0;5,0,0,0" /></SOLDIERS></STORE>')
            elif channel == 'C' and command == 'GETEXTDATA':
                # FriendlyGame.OnExtdataResult expects the EXTDATA container
                # even when the requested friend list is empty.
                out = reply('C', cid, mn, '<EXTDATA />')
            elif channel == 'C' and command == 'ADDSEPROOM':
                tag = roots[1]
                with friendly_lock:
                    # Creating again replaces the caller's old waiting room.
                    old_id = sessions[cid].get('friendly_room', '')
                    old = friendly_rooms.get(old_id)
                    if old and old.get('owner') == cid:
                        _friendly_finish(old)
                    elif old:
                        _friendly_detach(cid)
                    room_id = str(next_friendly_room); next_friendly_room += 1
                    used_codes = {str(r.get('code', '')) for r in friendly_rooms.values()}
                    code = ''
                    for _ in range(9000):
                        candidate = f'{1000 + secrets.randbelow(9000):04d}'
                        if candidate not in used_codes:
                            code = candidate; break
                    if not code: code = f'{1000 + (next_friendly_room % 9000):04d}'
                    slots: list[dict[str, object]] = [
                        {'cid': cid, 'kind': 'human'},
                    ]
                    human_limit = max(2, min(3, int(_settings().get('max_players_per_room', 3))))
                    possible_humans = 1
                    for slot_number, key in enumerate(('OPP1', 'OPP2'), 1):
                        opponent = tag.get(key, '-1')
                        if opponent == '-1': slots.append({'kind': 'bot'})
                        elif possible_humans >= human_limit:
                            slots.append({'kind': 'bot'})
                        elif opponent == '-2':
                            slots.append({'kind': 'code'}); possible_humans += 1
                        elif opponent in ('', '0'):
                            slots.append({'kind': 'open'}); possible_humans += 1
                        else:
                            # The room-list tag carries the invited id so that
                            # the stock client displays its accept dialog.
                            slots.append({'kind': 'invite', 'user_id': opponent,
                                          'name': tag.get(f'NAME{slot_number}', '')})
                            possible_humans += 1
                    room: dict[str, object] = {
                        'id': room_id, 'code': code, 'owner': cid,
                        'members': [cid], 'slots': slots,
                        'rules': tag.get('RULES', '2'), 'qcats': tag.get('QCATS', ''),
                        'generation': 1, 'started': False,
                    }
                    friendly_rooms[room_id] = room
                    sessions[cid].update({'friendly_room': room_id, 'screen': 'WAIT',
                                          'roomsel': '0'})
                    _friendly_mark(room)
                    _friendly_mark_lobbies()
                out = reply('C', cid, mn)
            elif channel == 'C' and command == 'ENTERSEPROOM':
                tag = roots[1]; wanted_code = tag.get('CODE', '').strip()
                wanted_room = tag.get('ROOM', '').strip()
                joined = False
                with friendly_lock:
                    room = next((r for r in friendly_rooms.values()
                                 if not r.get('started') and
                                 ((wanted_code and str(r.get('code')) == wanted_code) or
                                  (wanted_room and str(r.get('id')) == wanted_room))), None)
                    if room:
                        slot_index = -1
                        for index, slot in enumerate(room.get('slots', [])):
                            if not isinstance(slot, dict) or slot.get('cid'): continue
                            invited = (slot.get('kind') == 'invite' and
                                       str(slot.get('user_id')) == sessions[cid]['user_id'])
                            if slot.get('kind') in ('code', 'open') or invited:
                                slot_index = index; break
                        if slot_index >= 0:
                            old = friendly_rooms.get(sessions[cid].get('friendly_room', ''))
                            if old and old is not room: _friendly_detach(cid)
                            slot = room['slots'][slot_index]
                            assert isinstance(slot, dict)
                            slot.update({'cid': cid, 'kind': 'human'})
                            members = room.setdefault('members', [])
                            assert isinstance(members, list)
                            if cid not in members: members.append(cid)
                            sessions[cid].update({'friendly_room': str(room['id']),
                                                  'screen': 'WAIT', 'roomsel': '0'})
                            _friendly_mark(room); _friendly_mark_lobbies(); joined = True
                out = (reply('C', cid, mn) if joined else
                       f'<C CID="{cid}" MN="{mn}" R="68" />\r\n')
            elif channel == 'C' and command == 'EXITROOM':
                with friendly_lock:
                    room = friendly_rooms.get(sessions[cid].get('friendly_room', ''))
                    if room and room.get('owner') == cid:
                        _friendly_finish(room)
                    else:
                        _friendly_detach(cid)
                out = reply('C', cid, mn)
            elif channel == 'C' and command == 'STARTSEPROOM':
                started = False
                with friendly_lock:
                    room = friendly_rooms.get(sessions[cid].get('friendly_room', ''))
                    if room and room.get('owner') == cid and not room.get('started'):
                        # Empty invitation/code places become bots at launch.
                        for slot in room.get('slots', []):
                            if isinstance(slot, dict) and not slot.get('cid'):
                                slot.clear(); slot['kind'] = 'bot'
                        _friendly_game_setup(room); started = True
                out = (reply('C', cid, mn) if started else
                       f'<C CID="{cid}" MN="{mn}" R="68" />\r\n')
            elif channel == 'C' and command == 'CONFIRMSEPROOM':
                # Invite acceptance reaches here after switching to the game
                # lobby. Locate the reserved slot by the signed-in user id.
                joined = False
                with friendly_lock:
                    user_id = sessions[cid]['user_id']
                    room = next((candidate for candidate in friendly_rooms.values()
                                 if not candidate.get('started') and any(
                                     isinstance(slot, dict) and
                                     slot.get('kind') == 'invite' and
                                     str(slot.get('user_id')) == user_id
                                     for slot in candidate.get('slots', []))), None)
                    if room:
                        old = friendly_rooms.get(sessions[cid].get('friendly_room', ''))
                        if old and old is not room: _friendly_detach(cid)
                        for slot in room.get('slots', []):
                            if (isinstance(slot, dict) and slot.get('kind') == 'invite'
                                    and str(slot.get('user_id')) == user_id):
                                slot.update({'cid': cid, 'kind': 'human'}); break
                        members = room.setdefault('members', [])
                        if cid not in members: members.append(cid)
                        sessions[cid].update({'friendly_room': str(room['id']),
                                              'screen': 'WAIT', 'roomsel': '0'})
                        _friendly_mark(room); _friendly_mark_lobbies(); joined = True
                out = (reply('C', cid, mn) if joined else
                       f'<C CID="{cid}" MN="{mn}" R="68" />\r\n')
            elif channel == 'C' and command == 'CHANGEWAITHALL':
                wh = roots[1].get('WH', 'VILLAGE')
                sessions[cid]['screen'] = 'WAIT' if wh == 'GAME' else wh
                mark_dirty(sessions[cid])
                out = reply('C', cid, mn)
            elif channel == 'C' and command == 'ENTERROOM':
                sessions[cid]['screen'] = 'WAIT'
                sessions[cid]['roomsel'] = roots[1].get('ROOM', '1')
                sessions[cid]['start_at'] = str(time.time() + 3.0)
                mark_dirty(sessions[cid])
                out = reply('C', cid, mn)
            elif channel == 'C' and command == 'READY':
                room = _active_game_room(cid)
                if room:
                    with friendly_lock:
                        before = str(dict(room.get('game', {})).get('stage', ''))
                        delivered = (sessions[cid].get('sent_generation', '-1') ==
                                     sessions[cid].get('generation', '0'))
                        if delivered:
                            _friendly_advance_ready(room, cid)
                        game = room.get('game', {})
                        if (isinstance(game, dict) and before == 'free_result' and
                                game.get('stage') == 'gameover' and
                                not game.get('game_recorded')):
                            game_points = _game_points(game)
                            winner_slot = max(range(1, 4),
                                              key=lambda slot: game_points[slot - 1])
                            for member in _friendly_humans(room):
                                record_game_complete(
                                    sessions[member],
                                    won=_friendly_slot(room, member) == winner_slot)
                            game['game_recorded'] = True
                    out = reply('C', cid, mn)
                # PREPAREGAME finishes by sending READY. Advance once to base
                # selection; later READY packets merely acknowledge animation.
                elif sessions[cid].get('game_stage') == 'prepare':
                    sessions[cid]['game_stage'] = 'select'
                    mark_dirty(sessions[cid])
                elif sessions[cid].get('game_stage') == 'select_result':
                    sessions[cid]['game_stage'] = 'free0'
                    mark_dirty(sessions[cid])
                elif sessions[cid].get('game_stage') == 'free0':
                    sessions[cid]['game_stage'] = 'free_question'
                    mark_dirty(sessions[cid])
                elif sessions[cid].get('game_stage') == 'tip_result':
                    sessions[cid]['game_stage'] = 'free_select'
                    mark_dirty(sessions[cid])
                elif sessions[cid].get('game_stage') == 'free_result':
                    claims = [x for x in sessions[cid].get('claims', '').split(',') if x]
                    round_limit = min(MATCH_ROUNDS, MAP_AREA_COUNT - 3)
                    sessions[cid]['game_stage'] = ('gameover' if len(claims) >= round_limit
                                                   else 'free0')
                    if sessions[cid]['game_stage'] == 'gameover': record_game_complete(sessions[cid])
                    mark_dirty(sessions[cid])
                elif sessions[cid].get('game_stage') == 'gameover':
                    sessions[cid]['game_stage'] = 'aftergame'
                    mark_dirty(sessions[cid])
                out = reply('C', cid, mn)
            elif channel == 'C' and command == 'SELECT':
                raw_area = int(roots[1].get('AREA', '0'))
                area = max(1, min(MAP_AREA_COUNT, raw_area or 1))
                room = _active_game_room(cid)
                if room:
                    with friendly_lock:
                        game = room.get('game', {})
                        slot = _friendly_slot(room, cid)
                        if isinstance(game, dict) and slot == int(game.get('next_player', 0)):
                            occupied = {int(value) for value in dict(game.get('bases', {})).values()}
                            occupied.update(int(value) for value in dict(game.get('claims', {})).keys())
                            if raw_area == 0 or area in occupied:
                                area = next((n for n in range(1, MAP_AREA_COUNT + 1)
                                             if n not in occupied), 1)
                            if game.get('stage') == 'select':
                                game.setdefault('bases', {})[str(slot)] = area
                                game.update({'last_player': slot, 'stage': 'select_result',
                                             'ready': set()})
                                _friendly_mark(room)
                            elif game.get('stage') == 'free_select':
                                game.setdefault('claims', {})[str(area)] = slot
                                game.update({'free_area': area, 'winner': slot,
                                             'stage': 'free_result', 'ready': set()})
                                _friendly_mark(room)
                    out = reply('C', cid, mn)
                elif sessions[cid].get('game_stage') == 'select':
                    choices = [n for n in range(1, MAP_AREA_COUNT + 1) if n != area]
                    sessions[cid].update({
                        'selected_area': str(area), 'bot2_area': str(choices[5]),
                        'bot3_area': str(choices[11]), 'game_stage': 'select_result',
                    })
                    mark_dirty(sessions[cid])
                else:
                    occupied = {int(x) for x in (
                        sessions[cid].get('selected_area', '0'),
                        sessions[cid].get('bot2_area', '0'),
                        sessions[cid].get('bot3_area', '0')) if x != '0'}
                    occupied.update(int(x) for x in sessions[cid].get('claims', '').split(',') if x)
                    if raw_area == 0 or area in occupied:
                        area = next(n for n in range(1, MAP_AREA_COUNT + 1)
                                    if n not in occupied)
                    sessions[cid]['free_area'] = str(area)
                    claims = [x for x in sessions[cid].get('claims', '').split(',') if x]
                    if str(area) not in claims: claims.append(str(area))
                    sessions[cid]['claims'] = ','.join(claims)
                    sessions[cid]['game_stage'] = 'free_result'
                    mark_dirty(sessions[cid])
                out = reply('C', cid, mn)
            elif channel == 'C' and command == 'TIP':
                room = _active_game_room(cid)
                if room:
                    with friendly_lock:
                        game = room.get('game', {})
                        if isinstance(game, dict) and game.get('stage') == 'free_question':
                            slot = _friendly_slot(room, cid)
                            question = _current_question(game)
                            good = int(question['answer'])
                            answer = roots[1].get('TIP', str(good))
                            answers = game.setdefault('answers', {})
                            assert isinstance(answers, dict)
                            answers[str(slot)] = answer
                            human_slots = {_friendly_slot(room, member)
                                           for member in _friendly_humans(room)}
                            if all(str(human_slot) in answers for human_slot in human_slots):
                                for bot_slot in range(1, 4):
                                    if str(bot_slot) not in answers:
                                        spread = 20 + 30 * int(question.get('difficulty', 1))
                                        offset = secrets.randbelow(spread * 2 + 1) - spread
                                        answers[str(bot_slot)] = str(good + offset)
                                def distance(candidate: int) -> tuple[int, int]:
                                    try: return abs(int(answers[str(candidate)]) - good), candidate
                                    except ValueError: return 10**9, candidate
                                game.update({'winner': min(range(1, 4), key=distance),
                                             'stage': 'tip_result', 'ready': set()})
                                _friendly_mark(room)
                else:
                    sessions[cid]['tip_answer'] = roots[1].get('TIP', '1348')
                    sessions[cid]['game_stage'] = 'tip_result'
                    mark_dirty(sessions[cid])
                out = reply('C', cid, mn)
            elif channel == 'C' and command == 'CLOSEGAME':
                with friendly_lock:
                    room = _active_game_room(cid)
                    if room and sessions[cid].get('friendly_room'):
                        _friendly_finish(room)
                    else:
                        sessions[cid].pop('solo_room', None)
                        sessions[cid].update({'screen': 'WAIT', 'roomsel': '0'})
                        mark_dirty(sessions[cid])
                out = reply('C', cid, mn)
            elif channel == 'C' and command == 'COLLECTTAX':
                reward, _ = _collect_tax(sessions[cid].get('user_id', ''))
                # MYDATA drives the castle coin animation and updates the
                # village HUD. Repeated taps during the cooldown grant zero.
                out = reply('C', cid, mn, player_data(sessions[cid]))
                print(f'CASTLE_TAX user={sessions[cid].get("user_id", "")} reward={reward}', flush=True)
            else:
                out = reply(channel, cid, mn)
            data = pack_frame(out)
            if not noisy_poll:
                print(f'TX {out!r}', flush=True)
            self.send_response(200); self.send_header('Content-Type','application/octet-stream'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
        except Exception as exc:
            self.send_error(400, str(exc))

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--host', default=os.environ.get('DOBYVATEL_BIND', '127.0.0.1'))
    p.add_argument('--port', type=int, default=int(os.environ.get('DOBYVATEL_PORT', '8080')))
    a=p.parse_args()
    ThreadingHTTPServer((a.host,a.port), Handler).serve_forever()
if __name__ == '__main__': main()
